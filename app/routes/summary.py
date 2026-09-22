"""``GET /summary.json`` — live portfolio KPIs (FIFO engine + live prices).

Every figure is computed on every request from the transactions table (no
caching, no static fallback), per API.md's ``/summary.json`` contract:

* All figures come straight out of :func:`app.accounting.compute_portfolio_metrics`
  — the same FIFO engine that feeds /holdings.json and /outlook*.json.
* Every numeric field is a JSON **number** (dollars rounded to 2 decimals,
  ``*Pct`` fields to 1 decimal) — never pre-formatted strings; that's the
  /transactions.json world.
* Live prices come from Yahoo Finance in one batched call for every symbol
  still held; any symbol that fails to resolve falls back to its most recent
  transaction price rather than crashing or returning zero, and ``priceAsOf``
  carries a ``stale`` marker whenever any fallback occurred (API.md: ISO-8601
  UTC timestamp of the fetch with "stale" appended).

Response shape is field-for-field what API.md documents (portfolioValue,
costBasis, unrealizedGainLoss(+Pct), realizedGainLoss, dividendsTotal,
gainLossInclDividends(+Pct), cashAvailable, totalDeposited, holdingsCount,
allocation.{stocksPct,cashPct}, winLoss.{totalWin,totalLoss,winRatePct},
priceAsOf).
"""

from datetime import datetime, timezone

from fastapi import APIRouter

from app.accounting import compute_portfolio_metrics
from app.cache import cached_response
from app.routes.holdings import _load_transaction_rows

router = APIRouter()


def _utc_z_timestamp() -> str:
    """ISO-8601 UTC "now" with a ``Z`` suffix (API.md's priceAsOf example
    shape, e.g. ``2026-05-31T14:02:11Z``)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _utc_z_timestamp_from(value: str) -> str:
    """Rewrite a ``+00:00``-suffixed ISO timestamp to the documented ``Z`` shape,
    preserving any trailing annotation (the stale-fallback note)."""
    return value.replace("+00:00", "Z")


@router.get("/summary.json")
@cached_response("summary.json")
async def summary_json():
    """Single portfolio-KPI object; every numeric field a JSON number."""
    from app import market_data

    rows = _load_transaction_rows()
    metrics = compute_portfolio_metrics(rows)

    # Live prices for every symbol that still has shares (shares > 1e-6, the
    # same threshold the engine uses for holdingsCount); last-transaction
    # prices are the fallback layer inside fetch_live_prices.
    held_symbols = [
        sym for sym, shares in metrics["remaining"].items() if shares > 1e-6
    ]
    if held_symbols:
        fallbacks = market_data.last_transaction_prices(held_symbols)
        prices, price_as_of = market_data.fetch_live_prices(
            held_symbols, fallback_prices=fallbacks
        )
        # API.md documents the Z-suffix shape (e.g. 2026-05-31T14:02:11Z);
        # fetch_live_prices returns the +00:00 variant — normalize it back.
        price_as_of = _utc_z_timestamp_from(price_as_of)
    else:
        prices, price_as_of = {}, _utc_z_timestamp()

    portfolio_value = 0.0
    for sym in held_symbols:
        # Symbols that could not be valued at all are absent from `prices`
        # (a warning is already logged inside fetch_live_prices) — value them as 0.
        shares = metrics["remaining"][sym]
        portfolio_value += shares * prices.get(sym, 0.0)

    cost_basis = metrics["costBasis"]
    unrealized_gain_loss = portfolio_value - cost_basis
    unrealized_pct = (
        round((unrealized_gain_loss / cost_basis) * 100, 1) if cost_basis > 0 else 0.0
    )

    gain_loss_incl_dividends = (
        unrealized_gain_loss + metrics["realizedGainLoss"] + metrics["dividendsTotal"]
    )
    incl_pct = (
        round((gain_loss_incl_dividends / cost_basis) * 100, 1) if cost_basis > 0 else 0.0
    )

    total_account_value = portfolio_value + metrics["cashAvailable"]
    stocks_pct = (
        round((portfolio_value / total_account_value) * 100, 1)
        if total_account_value != 0
        else 0.0
    )

    sell_count = metrics["sellCount"]
    win_rate_pct = (
        round(metrics["winningSellCount"] / sell_count * 100, 1)
        if sell_count > 0
        else 0.0
    )

    return {
        "portfolioValue": round(portfolio_value, 2),
        "costBasis": round(cost_basis, 2),
        "unrealizedGainLoss": round(unrealized_gain_loss, 2),
        "unrealizedGainLossPct": unrealized_pct,
        "realizedGainLoss": round(metrics["realizedGainLoss"], 2),
        "dividendsTotal": round(metrics["dividendsTotal"], 2),
        "gainLossInclDividends": round(gain_loss_incl_dividends, 2),
        "gainLossInclDividendsPct": incl_pct,
        "cashAvailable": round(metrics["cashAvailable"], 2),
        "totalDeposited": round(metrics["totalDeposited"], 2),
        "holdingsCount": metrics["holdingsCount"],
        "allocation": {
            "stocksPct": stocks_pct,
            "cashPct": round(100 - stocks_pct, 1),
        },
        "winLoss": {
            "totalWin": round(metrics["totalWin"], 2),
            "totalLoss": round(metrics["totalLoss"], 2),
            "winRatePct": win_rate_pct,
        },
        "priceAsOf": price_as_of,
    }
