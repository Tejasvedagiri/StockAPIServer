"""Portfolio-view endpoints: ``GET /holdings.json`` (and, in later steps,
``/sectors.json``, ``/sectorHoldings.json`` and ``/growth.json``).

All of these share one pipeline — load the raw ledger rows, resolve live
prices + DB-first symbol classifications once (:func:`_resolve_live_context`),
then hand that context to a pure shaping function in :mod:`app.accounting`.
The shape rules come straight from API.md: every numeric field is a JSON
*number* (dollars 2-decimals, percentages 1-2 decimals — no pre-formatted
strings here; that's the /transactions.json world).
"""

import logging

from fastapi import APIRouter

from app.cache import cached_response

logger = logging.getLogger(__name__)

router = APIRouter()


def _load_transaction_rows():
    """All ledger rows as plain dicts with string values, ``sym``-keyed.

    The FIFO engine (:mod:`app.accounting`) expects dict rows keyed exactly
    like the Robinhood CSVs (``"sym"``, not ``"symbol"``), so ORM attributes
    are mapped by hand here rather than dumping model objects.
    """
    from app.db import SessionLocal
    from app.models import Transaction

    session = SessionLocal()
    try:
        rows = [row for row in session.query(Transaction).all()]
    finally:
        session.close()

    return [
        {
            "activity_date": str(row.activity_date or ""),
            "sym": str(row.symbol or "").strip().upper(),
            "trans_code": str(row.trans_code or ""),
            "quantity": str(row.quantity or ""),
            "price": str(row.price or ""),
            "amount": str(row.amount or ""),
        }
        for row in rows
    ]


def _resolve_live_context(rows):
    """Resolve (prices, classifications) once for a batch of raw txn dicts.

    Mirrors StockMcpServer's ``_resolve_live_context``: held symbols are the
    distinct non-empty ``sym`` values; per-symbol last-transaction prices act
    as the fallback layer inside :func:`app.market_data.fetch_live_prices` so
    every symbol ends up valued even when Yahoo has nothing for it. Classifications
    go through the DB-first read-through (stock_info table, yfinance only on
    miss), so repeat requests are cheap and rate-limit-safe.
    """
    from app import market_data

    held = {r["sym"] for r in rows if r["sym"]}
    fallbacks = market_data.last_transaction_prices(held)
    prices, _price_as_of = market_data.fetch_live_prices(held, fallback_prices=fallbacks)
    classifications = market_data.get_symbol_classifications(held)
    return prices, classifications


@router.get("/holdings.json")
@cached_response("holdings.json")
async def holdings_json():
    """Bare JSON array of one row per currently-held symbol (net shares above
    the negligible threshold), sorted by market value descending.

    Field-by-field per API.md: ``sym``/``name``/``sector``/``assetType`` are
    strings; ``shares``/``avgCost``/``price``/``marketValue``/``gainPct`` are
    JSON *numbers* — never pre-formatted. The pure shaping lives in
    :func:`app.accounting.build_holdings_breakdown`; this handler only loads
    rows, resolves live context and delegates.
    """
    from app.accounting import build_holdings_breakdown

    rows = _load_transaction_rows()
    prices, classifications = _resolve_live_context(rows)
    return build_holdings_breakdown(rows, prices, classifications)


#: The UI's existing sector palette (var(--sN), in order) — reused verbatim,
#: just redistributed across however many buckets actually exist per request.
_SECTOR_COLORS = [
    "var(--s1)",
    "var(--s2)",
    "var(--s3)",
    "var(--s4)",
    "var(--s5)",
    "var(--s6)",
    "var(--s7)",
    "var(--s8)",
    "var(--s9)",
]


def _bucket_label(holding: dict) -> str:
    """API.md requires /sectors.json labels and /sectorHoldings.json keys to
    match exactly; both come from the same rule — real sector, falling back
    to asset type, then a generic bucket."""
    return holding["sector"] or holding["assetType"] or "Other"


def _parse_dollar_string(s: str) -> float:
    """``"$1,234.56"`` → ``1234.56`` — keeps in-bucket sorting independent of locale."""
    return float(s.replace("$", "").replace(",", ""))


@router.get("/sectors.json")
@cached_response("sectors.json")
async def sectors_json():
    """Sector/asset-type allocation as an array of ``{label, pct, color}``,
    sorted by market value descending.

    Per API.md: ``pct`` is a JSON *number* (bucket market value ÷ total × 100,
    never a pre-formatted string) and ``color`` is a CSS value the frontend
    applies verbatim — one of the UI's existing ``var(--sN)`` palette slots,
    with a generated hsl() color for any bucket beyond the nine slots.
    """
    from app.accounting import build_holdings_breakdown

    rows = _load_transaction_rows()
    prices, classifications = _resolve_live_context(rows)
    breakdown = build_holdings_breakdown(rows, prices, classifications)

    buckets: dict[str, float] = {}
    total_value = 0.0
    for h in breakdown:
        label = _bucket_label(h)
        buckets[label] = buckets.get(label, 0.0) + h["marketValue"]
        total_value += h["marketValue"]

    if not buckets:
        return []

    out = []
    for i, (label, value) in enumerate(
        sorted(buckets.items(), key=lambda kv: -kv[1])
    ):
        color = _SECTOR_COLORS[i] if i < len(_SECTOR_COLORS) else f"hsl({i * 40 % 360}, 55%, 50%)"
        out.append(
            {"label": label, "pct": round(value / total_value * 100, 2), "color": color}
        )
    return out


@router.get("/sectorHoldings.json")
@cached_response("sectorHoldings.json")
async def sector_holdings_json():
    """Per-sector drill-down: an OBJECT keyed by the same bucket labels as
    ``/sectors.json``, each value an array of holding rows sorted by market
    value descending within the bucket.

    Per API.md these rows are pre-formatted strings — ``value`` is
    ``"$X,XXX.XX"`` and ``change`` a signed percent with one decimal (e.g.
    ``"+8.2%"`` / ``"-2.3%"``) — plus ``pos``, a real JSON boolean for the
    green/red styling. This string formatting is specific to this endpoint;
    /holdings.json keeps its numbers numeric.
    """
    from app.accounting import build_holdings_breakdown

    rows = _load_transaction_rows()
    prices, classifications = _resolve_live_context(rows)
    breakdown = build_holdings_breakdown(rows, prices, classifications)

    by_bucket: dict[str, list[dict]] = {}
    for h in breakdown:
        label = _bucket_label(h)
        entry = {
            "sym": h["sym"],
            "name": h["name"],
            "value": f"${h['marketValue']:,.2f}",
            "change": (
                ("+" if h["gainPct"] >= 0 else "-") + f"{abs(h['gainPct']):.1f}%"
            ),
            "pos": h["gainPct"] > 0,
        }
        by_bucket.setdefault(label, []).append(entry)

    for entries in by_bucket.values():
        entries.sort(key=lambda e: -_parse_dollar_string(e["value"]))

    return by_bucket


@router.get("/growth.json")
@cached_response("growth.json")
async def growth_json():
    """Per-holding performance as an array of ``{sym, pct}``.

    Per API.md: ``pct`` is a JSON *number* (positive or negative; the
    frontend appends ``%`` itself) — never a pre-formatted string. Rows follow
    the breakdown's own market-value-descending order.
    """
    from app.accounting import build_holdings_breakdown

    rows = _load_transaction_rows()
    prices, classifications = _resolve_live_context(rows)
    breakdown = build_holdings_breakdown(rows, prices, classifications)
    return [{"sym": h["sym"], "pct": round(h["gainPct"], 2)} for h in breakdown]
