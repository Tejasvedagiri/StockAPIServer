"""``GET /transactions.json`` — raw ledger rows with mapped type + reformatted date.

Every field in the response is a pre-formatted *string* (API.md): dates as
``Mon D, YYYY``, money as ``$X,XXX.XX`` / signed amounts, and the em-dash
placeholder ``"—"`` wherever a share count or price doesn't apply for the row's
type. The GET handler itself lands in the next plan step; this module currently
holds only its shaping helpers so they can be smoke-checked in isolation.
"""

import logging
import re
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter

from app.cache import cached_response

logger = logging.getLogger(__name__)

router = APIRouter()

#: The em-dash placeholder API.md uses wherever a share count or price doesn't
#: apply for the row's type (dividends, transfers, interest). U+2014 exactly —
#: the UI compares against this literal character.
EM_DASH = "—"


def _format_activity_date(raw: str) -> str:
    """``'M/D/YYYY'`` -> ``'Mon D, YYYY'`` (delegates to the accounting engine's
    canonical reformat; unparseable input passes through verbatim with a warning)."""
    from app.accounting import format_activity_date

    return format_activity_date(raw)


def _activity_sort_key(raw: str):
    """Newest-first sort key for raw Activity Date strings.

    Delegates to the accounting engine's ``activity_date_key`` (parsed datetime,
    unparseable dates last) and inverts the date so a normal *ascending* Python
    sort yields newest-first order while keeping rows stable within a day —
    matching API.md's "most recent first" ledger listing.
    """
    from app.accounting import activity_date_key

    tag, value = activity_date_key(raw)
    # Invert the datetime so newer dates sort earlier; unparseable (tag=1) rows
    # keep sorting last regardless of inversion direction. A constant second
    # element for the unparseable case keeps Python's stable sort from reordering
    # those rows lexicographically — they stay in input order, at the very end.
    if tag == 0:
        return (0, -value.timestamp())
    return (1, 0)


def _parse_money(raw: str):
    """Split a raw Robinhood money string into ``(Decimal value, is_negative)``.

    Handles every shape the CSVs use: bare ``$1,208.40``, leading-minus
    ``-$3,890.00``, and the parentheses variant ``($2,000.00)``. Returns
    ``(None, False)`` for blank or unparseable input so callers can fall back
    to an em-dash instead of crashing the row.
    """
    text = str(raw or "").strip()
    if not text:
        return None, False
    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    digits = text.replace("$", "").replace(",", "")
    for ch in ("-", "(", ")"):
        digits = digits.replace(ch, "")
    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None, False
    return value, negative


def _format_money(raw: str) -> str:
    """Re-render a raw Robinhood money string unsigned (API.md's `price` field).

    ``$1,208.40`` stays as-is; negatives keep their sign — ``-$3,890.00`` and
    the parentheses variant ``($2,000.00)`` both re-render with a leading
    minus. Blank or unparseable input falls back to an em-dash so no row ever
    carries a hole. Used for the `price` column, which API.md documents as
    unsigned ("$X,XXX.XX").
    """
    value, negative = _parse_money(raw)
    if value is None:
        return EM_DASH
    out = f"{value:,.2f}"
    if negative and value != 0:
        # The sign comes BEFORE the dollar sign ("-$1,208.40"), never after.
        return "-$" + out
    return "$" + out


def _format_signed_amount(raw: str) -> str:
    """Re-render a raw Robinhood amount string with an explicit +/- sign.

    API.md's /transactions.json documents `amount` as pre-formatted **signed**
    currency — every worked example carries one ("-$2,634.00" for a buy,
    "+$84.60" for a dividend). The DB stores bare ``$`` values (negatives in
    parentheses), so the sign is derived here: negative input -> "-$", positive
    -> "+$". Blank or unparseable input falls back to an em-dash.
    """
    value, negative = _parse_money(raw)
    if value is None:
        return EM_DASH
    out = f"{value:,.2f}"
    prefix = "-$" if (negative and value != 0) else "+$"
    return prefix + out


def _format_zero_price() -> str:
    """The "$0.00" price placeholder for SPL split rows (source Price is blank)."""
    return "$0.00"


def _render_spl_rec_row(row, mapped_type: str) -> dict:
    """Shape the 5 Robinhood rows whose source Price AND Amount are both empty.

    SPL buy rows (stock splits) keep their real numeric share count and get a
    $-formatted "$0.00" price — documented placeholder, since a split has no
    cost basis; REC dividend rows get an em-dash share count and parse the $
    figure out of the Description when one is present, else the same "$0.00"
    placeholder (this CSV's 2 REC rows carry none). API.md only allows "—"
    where a value genuinely doesn't apply — price/amount do apply here, so no
    em-dash on those fields for these codes.
    """
    if mapped_type == "buy":
        return {
            "type": mapped_type,
            "sym": row.symbol or "",
            "date": _format_activity_date(str(row.activity_date or "")),
            # Splits really do move shares; render the real count.
            "shares": _format_shares(row.quantity),
            "price": _format_zero_price(),  # source Price empty — documented placeholder, not a missing value
            "amount": "+$0.00",             # splits carry no cash (source Amount empty)
        }
    return {
        "type": mapped_type,  # dividend (REC maps to dividend via map_trans_code)
        "sym": row.symbol or "",
        "date": _format_activity_date(str(row.activity_date or "")),
        "shares": EM_DASH,                     # dividends are not share counts
        "price": EM_DASH,                      # no per-share price for a cash receipt
        "amount": _rec_dividend_amount(row.description),  # $ figure in Description if any, else placeholder
    }


def _rec_dividend_amount(description: str) -> str:
    """Pull the dollar figure out of a REC row's Description, or fall back to "$0.00".

    This CSV's two REC Descriptions are just "Company Name\\nCUSIP: …" with no
    $ amount (verified data gap), so they render as "+$0.00"; the parse is kept
    for any future export that does include one.
    """
    text = str(description or "")
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", text)
    if not m:
        return "+$0.00"  # no $ figure in the source — documented placeholder for this CSV data gap
    try:
        value = Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return "+$0.00"
    return ("+" if value >= 0 else "-") + "$" + f"{abs(value):,.2f}"


@router.get("/transactions.json")
@cached_response("transactions.json")
async def transactions_json():
    """Bare JSON array of all ledger rows, newest first.

    Every field is a pre-formatted *string* (API.md): ``type`` mapped from the
    raw Trans Code via :func:`app.accounting.map_trans_code` (unknown codes ->
    ``"transfer"`` + warning, never crash or drop the row), ``date`` reformatted
    to ``Mon D, YYYY``, and em-dash placeholders wherever a share count or price
    doesn't apply for the row's type. Rows with a blank Trans Code are excluded
    from this endpoint (they still feed /summary.json's metrics).
    """
    from app.accounting import map_trans_code
    from app.db import SessionLocal
    from app.models import Transaction

    session = SessionLocal()
    try:
        rows = [row for row in session.query(Transaction).all()]
    finally:
        session.close()

    enriched = [r for r in rows if (r.trans_code or "").strip()]
    # Stable ascending sort on the inverted key => newest-first by parsed date,
    # original DB order preserved within a day; unparseable dates sort last.
    enriched.sort(key=lambda r: _activity_sort_key(r.activity_date or ""))

    out = []
    for row in enriched:
        mapped_type = map_trans_code(str(row.trans_code or ""))
        if (row.trans_code or "").strip().upper() in ("SPL", "REC"):
            # These Robinhood codes have blank Price AND Amount in the source;
            # give them their documented placeholders instead of em-dashes.
            out.append(_render_spl_rec_row(row, mapped_type))
        else:
            out.append(
                {
                    "type": mapped_type,
                    # API.md: "Ticker symbol -- or a label such as 'Bank
                    # ****4821' for transfers." Non-trade rows (ACH, interest,
                    # card, brokerage transfer) have a blank Instrument/symbol
                    # column in the source CSV -- Robinhood's own export
                    # doesn't include a masked account number, so a literal
                    # "Bank ****XXXX" would be fabricated. The real
                    # Description text is the honest substitute (and
                    # occasionally already names a real masked account, e.g.
                    # "Instant bank transfer - account ending in 9324").
                    "sym": (row.symbol or "").strip() or (row.description or "").strip(),
                    "date": _format_activity_date(str(row.activity_date or "")),
                    "shares": _format_shares(row.quantity) if row.quantity else EM_DASH,
                    "price": _format_money(row.price) if row.price else EM_DASH,
                    "amount": _format_signed_amount(row.amount),
                }
            )
    return out


def _format_shares(raw: str) -> str:
    """Render a raw quantity string as a share count, or an em-dash.

    Blank and non-numeric quantities (dividends, transfers, interest rows)
    become ``"—"`` per API.md; real counts are re-rendered through Decimal so
    trailing zeros drop (``"12.0"`` → ``12``, ``"0.140999"`` stays exact).
    """
    text = str(raw or "").strip()
    if not text:
        return EM_DASH
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return EM_DASH
    normalized = format(value.normalize(), "f")
    # Decimal().normalize() turns 0 into '0E-6' style exponents on some paths;
    # the "f" format above already avoids that, but keep a belt-and-suspenders.
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"
