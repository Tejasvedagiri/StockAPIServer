"""RSS news fetching for held symbols (Yahoo Finance, no API key).

Ported from StockMcpServer/src/stockmcpserver/routes/news.py. The single-feed
layer (:func:`fetch_feed` + helpers) is standalone and testable without the
HTTP route; :func:`collect_news` merges per-symbol feeds into the API.md
/newsItems.json item shape:

    {"badge": str(1 char), "color": "a"|"b"|"c"|"d", "headline": str,
     "snippet": str, "src": str, "time": str (relative display string),
     "hours": number, "tickers": [str]}

``tickers`` always includes the symbol whose feed served the item, plus any
other held tickers mentioned as whole words in the headline or snippet.
``hours`` is hours elapsed since publication as a real JSON number (the UI
filters with ``n.hours / 24 <= windowDays``); items without a parseable
pubDate get :data:`FALLBACK_HOURS` so they only appear in the largest time
windows. A symbol whose feed fails or returns nothing is skipped — one bad
holding must never drop the whole response, and a full network outage
degrades to ``[]`` rather than a 500.
"""

import asyncio
import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

MAX_ITEMS = 30
PER_SYMBOL_LIMIT = 10
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
_BADGE = "Y"  # presentational single letter (API.md example: one of A/N/S)
_COLORS = ("a", "b", "c", "d")
_SNIPPET_MAX_CHARS = 300
FALLBACK_HOURS = 24 * 31


def _clean_snippet(raw: str) -> str:
    """Strip HTML tags / the RSS 'Read More' link and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > _SNIPPET_MAX_CHARS:
        cut = text[:_SNIPPET_MAX_CHARS].rsplit(" ", 1)[0]
        return cut + "\u2026"
    return text


def fetch_feed(symbol: str) -> list[dict]:
    """Fetch and parse one symbol's RSS feed. Runs in a worker thread."""
    url = (
        f"https://feeds.finance.yahoo.com/rss/2.0/headline"
        f"?s={symbol}&count={PER_SYMBOL_LIMIT}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read()
    root = ET.fromstring(body)
    items: list[dict] = []
    for node in root.iter("item"):
        title = (node.findtext("title") or "").strip()
        if not title:
            continue
        raw_date = node.findtext("pubDate") or ""
        published_at: datetime | None = None
        try:
            parsed = parsedate_to_datetime(raw_date)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            published_at = parsed
        except (TypeError, ValueError):
            pass
        items.append(
            {
                "id": node.findtext("guid") or title,
                "headline": title,
                "published_at": published_at,
                "snippet": _clean_snippet(node.findtext("description") or ""),
            }
        )
    return items


def matching_symbols(text: str, held: list[str], source_symbol: str) -> list[str]:
    """Tickers for a news item: the feed's own symbol first (the item exists
    because Yahoo served it under that ticker), then any other held tickers
    mentioned as whole words in the text."""
    found = [source_symbol] if source_symbol else []
    for sym in held:
        if sym not in found and re.search(
            rf"\b{re.escape(sym)}\b", text, flags=re.IGNORECASE
        ):
            found.append(sym)
    return found


def _relative_time(hours: float) -> str:
    if hours < 1:
        minutes = max(1, int(round(hours * 60)))
        return f"{minutes}m ago"
    if hours < 24:
        return f"{int(hours)}h ago"
    days = int(hours // 24)
    return f"{days} day{'s' if days != 1 else ''} ago"


def _to_api_item(item: dict, held: list[str], now: datetime, source_symbol: str) -> dict:
    published_at = item.get("published_at")
    if published_at is not None and published_at <= now:
        hours = (now - published_at).total_seconds() / 3600.0
    else:
        hours = float(FALLBACK_HOURS)
    text = f"{item['headline']} {item['snippet']}"
    return {
        "badge": _BADGE,
        "color": _COLORS[hash(item["id"]) % len(_COLORS)],
        "headline": item["headline"],
        "snippet": item["snippet"],
        "src": "Yahoo Finance",
        "time": _relative_time(hours),
        "hours": round(hours),
        "tickers": matching_symbols(text, held, source_symbol),
    }


async def collect_news(held: list[str]) -> list[dict]:
    """Fetch every held symbol's feed concurrently and merge into one list.

    Public entry point (also exercised directly by the tests). Returns items
    in API.md shape, sorted newest-first, capped at :data:`MAX_ITEMS`.
    """
    if not held:
        return []

    async def _one(symbol: str) -> tuple[str, list[dict]]:
        try:
            feed = await asyncio.to_thread(fetch_feed, symbol)
        except Exception as exc:  # noqa: BLE001 - one bad feed must not sink the rest
            logger.warning("news fetch failed for %s: %s", symbol, exc)
            return symbol, []
        return symbol, feed

    results = await asyncio.gather(*[_one(s) for s in held])

    now = datetime.now(timezone.utc)
    seen: set[str] = set()
    merged: list[tuple[float, dict]] = []
    for sym, feed in results:
        for item in feed:
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            entry = _to_api_item(item, held, now, sym)
            merged.append((-entry["hours"], entry))  # newest first

    merged.sort(key=lambda pair: (pair[0], pair[1]["headline"]))
    return [entry for _, entry in merged[:MAX_ITEMS]]


# ---------------------------------------------------------------------------
# FastAPI route — GET /newsItems.json
# ---------------------------------------------------------------------------

from fastapi import APIRouter  # noqa: E402
from app.cache import cached_response  # noqa: E402

router = APIRouter()


def _load_transaction_rows():
    """All ledger rows as plain dicts with string values, ``sym``-keyed.

    Identical shape to :func:`app.routes.holdings._load_transaction_rows` —
    the FIFO engine (:mod:`app.accounting`) expects dict rows keyed exactly
    like the Robinhood CSVs (``"sym"``, not ``"symbol"``).
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


def held_symbols() -> list[str]:
    """Symbols with a positive net position, straight out of the FIFO engine."""
    from app.accounting import compute_portfolio_metrics

    metrics = compute_portfolio_metrics(_load_transaction_rows())
    return sorted(
        sym
        for sym, detail in metrics.get("perSymbol", {}).items()
        if float(detail.get("shares", 0)) > 1e-6
    )


@router.get("/newsItems.json")
@cached_response("newsItems.json")
async def news_items_json():
    """Bare JSON array of news items for the currently-held symbols.

    Items carry exactly API.md's documented shape (badge/color/headline/
    snippet/src/time/hours/tickers). An empty ledger or a full network
    outage degrades to ``[]`` rather than an error, so the UI can always
    render.
    """
    return await collect_news(held_symbols())
