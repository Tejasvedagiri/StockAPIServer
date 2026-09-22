"""``GET /outlookOn.json`` and ``GET /outlookOff.json`` — live long-term projections.

Both endpoints recompute the 5/10/15/20/25/30-year rows on every request from
REAL inputs: the FIFO engine's real remaining cost basis as the starting
principal, a documented conservative base growth rate (see
``app.accounting.compute_outlook_projections``), and — for the "On" variant
only — the real trailing-12-month dividend yield on cost as the reinvestment
driver.

The response keeps API.md's positional 4-element string-row shape:
``[horizon_label, growth_only_total, dividends_reinvested_total, total]`` —
no field names anywhere in the payload (documented quirk, preserved exactly).
"""

import re
from datetime import date, timedelta

from fastapi import APIRouter
from app.cache import cached_response

from app.accounting import (
    TRANS_CODE_TO_TYPE,
    compute_outlook_projections,
    compute_portfolio_metrics,
    parse_accounting_number,
)

router = APIRouter()


def _load_transaction_rows():
    """All ledger rows as plain dicts with string values, ``sym``-keyed — the
    same shape :mod:`app.accounting`'s FIFO engine eats (shared rule with
    :mod:`app.routes.holdings`)."""
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


def _ttm_dividends(rows):
    """Sum of dividend-type rows (Trans Code mapped to ``dividend``) within the
    trailing 12 months from today; unparseable dates are skipped."""
    cutoff = date.today() - timedelta(days=365)
    total = 0.0
    for row in rows:
        if TRANS_CODE_TO_TYPE.get(row.get("trans_code") or "") != "dividend":
            continue
        raw_date = str(row.get("activity_date") or "").strip()
        d = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", raw_date)
        if not d:
            continue
        try:
            row_date = date(int(d.group(3)), int(d.group(1)), int(d.group(2)))
        except ValueError:
            continue
        if row_date >= cutoff:
            total += parse_accounting_number(row.get("amount"))
    return total


def _projected_rows(variant: str):
    """Compute both outlook variants from the same real inputs and return the
    requested one.

    Starting principal = real remaining cost basis from the FIFO engine;
    dividend reinvestment rate ("On" variant only) = real TTM dividends on
    that same cost basis, exactly as /summary.json computes it."""
    rows = _load_transaction_rows()
    metrics = compute_portfolio_metrics(rows)
    cost_basis = metrics["costBasis"]
    ttm_yield_pct = (
        (_ttm_dividends(rows) / cost_basis) * 100 if cost_basis else 0.0
    )
    off_rows, on_rows = compute_outlook_projections(cost_basis, ttm_yield_pct)
    return on_rows if variant == "on" else off_rows


@router.get("/outlookOn.json")
@cached_response("outlookOn.json")
async def outlook_on_json():
    """Dividends-reinvested projection — computed live from the real cost
    basis + TTM dividend yield on cost (no static seed)."""
    return _projected_rows("on")


@router.get("/outlookOff.json")
@cached_response("outlookOff.json")
async def outlook_off_json():
    """No-reinvestment projection — computed live from the real cost basis
    (no static seed)."""
    return _projected_rows("off")
