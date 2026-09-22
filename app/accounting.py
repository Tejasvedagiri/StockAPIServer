"""Portfolio accounting: the FIFO engine and its parsing/formatting helpers.

Ported from StockMcpServer's stockmcpserver/accounting.py (verified reference
implementation of the same API.md contract) into this repo's SQLAlchemy-based
layout. The engine itself stays PURE — no I/O, no prices, no ORM imports at
module level — so it is unit-testable without a live server:

* ``map_trans_code``            -- Robinhood Trans Code -> the 4-value activity type
                                   (/transactions.json + which rows count as share transactions)
* ``format_activity_date``      -- raw 'M/D/YYYY' -> API.md's 'Mon D, YYYY' style
* ``parse_accounting_number``   -- '$1,700.00' / '($2,000.00)' -> float
* ``compute_portfolio_metrics`` -- the FIFO cost-basis engine (per-symbol lots,
                                   realized P&L, cash-flow totals)
* ``build_holdings_breakdown``  -- per-holding rows for /holdings.json & co.,
                                   reusing the engine's lot tracking (FIFO is NOT
                                   reimplemented here)
* ``compute_outlook_projections`` -- horizon rows for /outlookOn.json +
                                     /outlookOff.json, preserving API.md's
                                     documented positional-string-array quirk

The one DB-facing helper, :func:`transactions_from_db`, translates this repo's
SQLAlchemy ``Transaction`` rows into the raw-string dicts the engine consumes;
everything else in this module is framework-agnostic.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


# --- Transaction type mapping (shared by /transactions.json and the engine) --


#: Robinhood "Trans Code" -> /transactions.json "activity".
#: SPL (stock split: shares increase with no cash flow) maps to 'buy' as the
#: closest of the 4 categories -- a known approximation, not a bug.
TRANS_CODE_TO_TYPE = {
    "Buy": "buy",
    "Sell": "sell",
    # CDIV cash dividend; NRAT NRA tax withholding (part of the dividend cash
    # flow); INT brokerage cash interest; SLIP stock-lending income; GDBP Gold
    # Deposit Boost Payment; REC dividend reinvestment -- all investment income.
    "CDIV": "dividend",
    "NRAT": "dividend",
    "INT": "dividend",
    "SLIP": "dividend",
    "GDBP": "dividend",
    "REC": "dividend",
    # ACH bank deposit/withdrawal; XENT_CC Robinhood Credit Card payment; NOA
    # Robinhood Card transaction; RTP instant bank transfer; ITRF
    # brokerage-to-brokerage transfer -- money movement, not securities.
    "ACH": "transfer",
    "XENT_CC": "transfer",
    "NOA": "transfer",
    "RTP": "transfer",
    "ITRF": "transfer",
    "SPL": "buy",
}


def map_trans_code(trans_code: str) -> str:
    """Map a raw Robinhood Trans Code to the API's 4-value ``activity``.

    Known codes map per :data:`TRANS_CODE_TO_TYPE`; any unknown code (e.g. from
    a future CSV re-harvest) maps to ``'transfer'`` as a safe default and logs
    a warning -- never crashes, never drops the row.
    """
    mapped = TRANS_CODE_TO_TYPE.get(trans_code)
    if mapped is None:
        logger.warning(
            "Unknown Trans Code %r in transactions table; mapping to 'transfer'",
            trans_code,
        )
        return "transfer"
    return mapped


# --- Date parsing / formatting helpers ---------------------------------------


def format_activity_date(raw: str) -> str:
    """Reformat a raw ``'M/D/YYYY'`` activity date into API.md's ``'Mon D, YYYY'`` style.

    e.g. ``8/14/2026`` -> ``Aug 14, 2026``. Unparseable input is returned
    unchanged (with a warning) so one bad row never breaks the endpoint.
    """
    try:
        dt = datetime.strptime(raw, "%m/%d/%Y")
    except ValueError:
        logger.warning("Unparseable Activity Date %r; passing through verbatim", raw)
        return raw
    return f"{dt:%b} {dt.day}, {dt.year}"


def activity_date_key(raw: str):
    """Sort key for an Activity Date string: parsed datetime, or (None,) so
    unparseable dates sort last in a newest-first ordering."""
    try:
        return (0, datetime.strptime(raw, "%m/%d/%Y"))
    except ValueError:
        return (1, raw)


def chronological_date_key(raw_date: str):
    """Ascending sort key for an Activity Date string based on the PARSED
    datetime — never lexicographic ('1/10' must come after '1/5').

    Unparseable dates sort first chronologically (and keep their stable input
    order among themselves).
    """
    try:
        return (0, datetime.strptime(str(raw_date), "%m/%d/%Y"))
    except ValueError:
        return (1, 0)


# --- Number parsing -----------------------------------------------------------


def parse_accounting_number(raw: str | None) -> float:
    """Parse a Robinhood CSV number string into a ``float``.

    Handles the pre-formatted accounting format used throughout the exports:
    ``'$1,700.00'`` -> ``1700.0``, ``'($2,000.00)'`` (parenthesized = negative)
    -> ``-2000.0``, empty/whitespace string or ``None`` -> ``0.0``. One helper
    for Amount / Quantity / Price alike.
    """
    if raw is None:
        return 0.0
    text = str(raw).strip()
    if not text:
        return 0.0
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    value = float(text.replace("$", "").replace(",", ""))
    return -value if negative else value


# --- The FIFO engine ----------------------------------------------------------


def compute_portfolio_metrics(transactions: list[dict]) -> dict:
    """Core portfolio accounting engine (pure — no I/O, no prices).

    ``transactions`` is a list of dicts with keys ``activity_date`` (raw
    ``M/D/YYYY`` string), ``sym``, ``trans_code``, ``quantity``, ``price``,
    ``amount`` (all raw DB strings); the engine itself re-sorts by Activity
    Date ascending via :func:`chronological_date_key` (parsed datetime, never
    lexicographic) so FIFO lots are consumed chronologically.

    Per-symbol FIFO: Buy pushes a lot whose total cost is the actual cash paid
    (``abs(parsed Amount)`` — the authoritative executed cost, not
    Quantity*Price); SPL/REC push a zero-cost lot (documented approximation:
    adds shares without changing total cost basis); Sell consumes from the
    FRONT up to the sold quantity, charging each consumed portion at its own
    lot's cost-per-share.

    Returns ``{'remaining', 'realizedGainLoss', 'totalWin', 'totalLoss',
    'sellCount', 'costBasis', 'holdingsCount', 'dividendsTotal',
    'totalDeposited', 'cashAvailable'}`` — plus ``perSymbol``, a dict keyed by
    symbol of ``{'shares', 'costBasis'}`` for every symbol with any lots
    (including zero-remaining ones), so callers can expose per-symbol cost
    basis without re-running FIFO.
    """

    lots: dict[str, list[dict]] = {}
    realized_gain_loss = 0.0
    total_win = 0.0
    total_loss = 0.0
    winning_sell_count = 0
    sell_count = 0
    cash_available = 0.0
    dividends_total = 0.0
    total_deposited = 0.0

    for txn in sorted(
        transactions, key=lambda t: chronological_date_key(t["activity_date"])
    ):
        code = str(txn.get("trans_code") or "")
        symbol = str(txn.get("sym") or "").strip()
        quantity = parse_accounting_number(txn.get("quantity"))
        amount = parse_accounting_number(txn.get("amount"))

        if code == "Buy" and quantity > 1e-9:
            lots.setdefault(symbol, []).append(
                {"shares": quantity, "costBasis": abs(amount)}
            )
        elif code in ("SPL", "REC") and quantity > 1e-9:
            # Zero-cost lot: adds shares without changing total cost basis.
            lots.setdefault(symbol, []).append({"shares": quantity, "costBasis": 0.0})

        # Cash-flow metrics accumulated over ALL rows (Amount is already the
        # correctly-signed cash delta for every row).
        cash_available += amount
        mapped_type = TRANS_CODE_TO_TYPE.get(code)
        if mapped_type == "dividend":
            dividends_total += amount  # NRAT's negative withholding nets naturally
        if code == "ACH" and amount > 0:
            total_deposited += amount  # deposits only, never netted against withdrawals
        elif code == "Sell":
            sell_count += 1
            remaining_to_sell = quantity
            cost_of_shares_sold = 0.0
            queue = lots.get(symbol, [])
            while remaining_to_sell > 1e-9 and queue:
                front = queue[0]
                take = min(front["shares"], remaining_to_sell)
                if front["shares"] > 1e-9:
                    cost_per_share = front["costBasis"] / front["shares"]
                else:
                    cost_per_share = 0.0
                cost_of_shares_sold += take * cost_per_share
                front["shares"] -= take
                front["costBasis"] -= take * cost_per_share
                remaining_to_sell -= take
                if front["shares"] <= 1e-9:
                    queue.pop(0)

            realized = amount - cost_of_shares_sold
            realized_gain_loss += realized
            if realized > 0:
                total_win += realized
                winning_sell_count += 1
            else:
                total_loss += realized

    remaining = {
        symbol: sum(lot["shares"] for lot in queue)
        for symbol, queue in lots.items()
    }

    # Per-symbol detail straight from the same lot tracking (no re-run):
    # shares still held + cost basis of those remaining shares.
    per_symbol = {
        symbol: {
            "shares": remaining[symbol],
            "costBasis": sum(lot["costBasis"] for lot in lots[symbol]),
        }
        for symbol, queue in lots.items()
    }

    cost_basis = 0.0
    holdings_count = 0
    for symbol, shares in remaining.items():
        if shares > 1e-6:
            holdings_count += 1
            cost_basis += sum(lot["costBasis"] for lot in lots[symbol])

    return {
        "remaining": remaining,
        "realizedGainLoss": realized_gain_loss,
        "totalWin": total_win,
        "totalLoss": total_loss,
        "sellCount": sell_count,
        "winningSellCount": winning_sell_count,
        "costBasis": cost_basis,
        "holdingsCount": holdings_count,
        "dividendsTotal": dividends_total,
        "totalDeposited": total_deposited,
        "cashAvailable": cash_available,
        "perSymbol": per_symbol,
    }


# --- Shared per-holding breakdown (used by /holdings.json, /growth.json, -----
# --- /sectors.json and /sectorHoldings.json — one computation for all) -------

#: Net-share threshold below which a symbol is no longer "held" (same value the
#: FIFO engine uses for holdingsCount).
HOLDINGS_SHARE_THRESHOLD = 1e-6


def build_holdings_breakdown(
    transactions: list[dict],
    prices: dict[str, float],
    classifications: dict[str, dict],
) -> list[dict]:
    """Per-holding rows computed ONCE from the FIFO engine + live data.

    ``transactions`` — raw DB rows (same shape as :func:`compute_portfolio_metrics`);
    ``prices`` — ``{sym: float}`` resolved by the caller via market_data
    (live, with last-transaction-price fallback already applied);
    ``classifications`` — ``{sym: {sector, assetType, name}}`` from
    ``market_data.get_symbol_classifications``.

    Returns one row per currently-held symbol (net shares > 1e-6), sorted by
    market value DESCENDING::

        {"sym", "name", "shares", "avgCost", "price", "marketValue",
         "gainPct", "sector", "assetType"}

    Shares and cost basis come straight from the engine's per-symbol lot
    tracking (``metrics["perSymbol"]``) — FIFO is NOT reimplemented here.
    ``avgCost`` = remaining cost basis ÷ remaining shares; ``gainPct`` follows
    API.md's formula ``(price - avgCost) / avgCost * 100`` and is internally
    consistent with the exact price used for ``marketValue``. A missing live
    price values the holding at 0.0 rather than crashing.
    """
    metrics = compute_portfolio_metrics(transactions)

    rows: list[dict] = []
    for sym, detail in metrics["perSymbol"].items():
        shares = detail["shares"]
        if shares <= HOLDINGS_SHARE_THRESHOLD:
            continue
        cost_basis = detail["costBasis"]
        avg_cost = (cost_basis / shares) if shares else 0.0
        price = prices.get(sym) or 0.0
        market_value = shares * price
        gain_pct = ((price - avg_cost) / avg_cost * 100) if avg_cost else 0.0
        cls = classifications.get(sym) or {}

        rows.append(
            {
                "sym": sym,
                "name": str(cls.get("name") or sym),
                "shares": round(shares, 4),
                "avgCost": round(avg_cost, 4),
                "price": round(price, 2),
                "marketValue": round(market_value, 2),
                "gainPct": round(gain_pct, 2),
                "sector": str(cls.get("sector") or "Other"),
                "assetType": str(cls.get("assetType") or "Stock"),
            }
        )

    rows.sort(key=lambda r: r["marketValue"], reverse=True)
    return rows


# --- Long-term projection (used by /outlookOn.json and /outlookOff.json) -----

#: Projection horizons in years — same set the API.md example body uses.
PROJECTION_HORIZONS = [5, 10, 15, 20, 25, 30]


def _usd(value: float) -> str:
    """Format a dollar amount as ``'$1,234.56'`` (API.md money-string style)."""
    return f"${value:,.2f}"


def compute_outlook_projections(
    cost_basis: float,
    ttm_dividend_yield_pct: float,
) -> tuple[list[list[str]], list[list[str]]]:
    """Long-term projection rows for the "Off" and "On" outlook endpoints.

    Both variants return a list of POSITIONAL 4-element STRING arrays
    ``[horizon_label, dividends_total, reinvested_or_growth_total,
    projected_total]`` — preserving the exact (quirky) shape API.md documents.
    The two variants must give DIFFERENT numbers (this is the whole point of
    the "Reinvest" toggle) — a prior version of this function accidentally
    returned the identical row for both, which this fixes:

    * column 1 (both variants — the dividends themselves don't depend on
      whether they're reinvested): cumulative dividend CASH received over
      the horizon, a simple (non-compounding) projection at the current
      TTM yield-on-cost — consistent with this function's own "conservative,
      documented, not one 'true' forecast" approach below.
    * column 2:
        - "Off" (not reinvested): principal grown at the base rate ALONE —
          dividends are received as cash on the side, not compounding.
        - "On" (reinvested): principal grown at the full (base + dividend)
          rate — dividends compound alongside the portfolio instead of
          sitting outside it.
    * column 3 (projected total):
        - "Off": base growth plus the dividend cash collected separately
          (they were never reinvested, so they just add on top).
        - "On": the single reinvested-growth figure alone (already includes
          the dividends' contribution via the boosted rate — adding column 1
          again would double-count them).

    Methodology (documented per todo_fix.md fix #4 — the requirement is real
    STARTING NUMBERS plus a documented method, not one "correct" forecast):

    * Starting principal = the REAL remaining cost basis from the FIFO engine
      (~$51,806.57 at audit time), NOT the ~$216K basis of the old fake seed
      table — so every figure scales down sensibly versus that mockup.
    * Base growth rate: a clearly-labeled conservative market-average
      assumption of 7%/year nominal (the long-run S&P 500 historical average).
      The account's own realized+unrealized history is too short and skewed to
      extrapolate responsibly — the ledger shows only ~$2,145 total deposited
      against a ~$51.8K cost basis (most of it recent, concentrated buys), so
      its own CAGR would imply an absurd 30-year multiple rather than a
      defensible forecast.
    * Dividend component ("On" variant only): the REAL trailing-12-month
      dividend yield on cost (~0.50% TTM at audit time, computed live by the
      caller as ``ttm_dividends / cost_basis``) added to the base rate — i.e.
      dividends are assumed reinvested and compounding alongside growth.

    Compounding is annual (once per year, horizon years of it).
    """
    BASE_ANNUAL_GROWTH = 0.07  # conservative market-average assumption (see docstring)
    div_rate = max(ttm_dividend_yield_pct, 0.0) / 100.0

    off_rows: list[list[str]] = []
    on_rows: list[list[str]] = []
    for years in PROJECTION_HORIZONS:
        growth_only = cost_basis * ((1 + BASE_ANNUAL_GROWTH) ** years)
        with_dividends = cost_basis * (
            (1 + BASE_ANNUAL_GROWTH + div_rate) ** years
        )
        dividends_cash = cost_basis * div_rate * years  # simple, non-compounding projection

        off_rows.append(
            [f"{years} years", _usd(dividends_cash), _usd(growth_only),
             _usd(growth_only + dividends_cash)]
        )
        on_rows.append(
            [f"{years} years", _usd(dividends_cash), _usd(with_dividends),
             _usd(with_dividends)]
        )
    return off_rows, on_rows


# --- SQLAlchemy adapter (the only DB-facing part of this module) -------------


def transactions_from_db(session) -> list[dict]:
    """Load every ``transactions`` row as the raw-string dicts the engine eats.

    Column mapping: the ORM's ``symbol`` column feeds the engine's ``sym`` key
    (the reference engine's historical name); everything else is passed through
    verbatim, so no parsing happens here — :func:`parse_accounting_number` and
    the date helpers stay responsible for that.

    Rows are returned in stable table order; the engine re-sorts by parsed
    Activity Date itself, so input order never affects the math.
    """
    from app.models import Transaction  # local import: keeps this module pure-importable

    rows = session.query(Transaction).all()
    return [
        {
            "activity_date": r.activity_date or "",
            "sym": (r.symbol or "").strip(),
            "trans_code": str(r.trans_code or ""),
            "quantity": r.quantity or "",
            "price": r.price or "",
            "amount": r.amount or "",
        }
        for r in rows
    ]
