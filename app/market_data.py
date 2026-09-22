"""Live market-data access (Yahoo Finance via the ``yfinance`` library).

Ported from StockMcpServer/src/stockmcpserver/market_data.py, translated to
this repo's SQLAlchemy idioms (app.db.SessionLocal / app.models.StockInfo)
instead of raw sqlite3. Logic is otherwise unchanged:

- :func:`get_symbol_classifications` reads the ``stock_info`` table FIRST; a
  symbol present there NEVER triggers a live yfinance info call. An in-process
  memo sits in front even of that DB read. Live yfinance is consulted only for
  symbols missing from both layers, and its result is upserted into
  ``stock_info`` so every later lookup — including in a brand-new process with
  an empty cache — is DB-only.
- :func:`fetch_live_prices` returns ``(prices, priceAsOf)``: one batched
  yfinance call for current prices, per-symbol fallback to the last
  transaction price (caller-supplied ``fallback_prices``) when the live quote
  is missing/zero, and a stale-suffix on priceAsOf whenever any symbol fell
  back.

Return contracts match StockMcpServer's exactly — see that module for the full
rationale; this file keeps them byte-for-byte in behavior so the endpoint
shaping rules built on top keep working unchanged.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import yfinance as yf
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.cache import DEFAULT_TTL, TTLCache
from app.db import Base, SessionLocal, engine
from app.models import StockInfo

logger = logging.getLogger(__name__)

# Shared in-memory cache for live yfinance ``info`` dicts. One bounded LRU+TTL
# instance is shared by every caller of :func:`_cached_fetch_all_info` so the 5
# independent price-fetching endpoints serve overlapping symbol sets from a
# single entry (see todo_cache.md). In-memory only — cleared on process restart.
_price_cache = TTLCache(max_size=64)


def _price_cache_key(symbols: list[str]) -> tuple:
    """Build the cache key for a batch price/info fetch.

    Symbols are sorted so that any permutation of the same symbol set maps to
    one shared entry — order must never fragment the cache (todo_cache.md).
    The leading ``"prices"`` tag namespaces this layer against other consumers
    of a TTLCache instance.
    """
    return ("prices",) + tuple(sorted(symbols))


# Overall wall-clock budget for one price-fetch round (mirrors the old raw-HTTP
# implementation's 90s cap; a batched Tickers call is far faster in practice).
PRICE_FETCH_OVERALL_BUDGET_SECONDS = 90.0

# Last-resort fallback classification — used only when yfinance fails to return
# sector/category for a symbol (a warning naming the symbol is logged; the
# endpoints never crash on an unmapped symbol).
SECTOR_MAP: dict[str, str] = {
    # ETFs get their own bucket, consistent with the original 9-sector scheme's "ETF" bucket
    "SCHD": "ETF", "SCHH": "ETF", "VTV": "ETF", "IAU": "ETF", "VBR": "ETF", "QQQ": "ETF",
    "KO": "Consumer Defensive",
    "CRSR": "Technology",
    "KLAR": "Financial Services",
    "MAIN": "Financial Services",
    "RVI": "Financial Services",   # Robinhood Ventures Fund I -- a closed-end investment fund, not a REIT
    "RVII": "Financial Services",  # Robinhood Ventures Fund II -- same as above
    "HOOD": "Financial Services",
    "PANW": "Technology",
    "PLTR": "Technology",
    "NVDA": "Technology",   # closed position, include only if still shown historically
    "JNJ": "Healthcare",    # closed position, same caveat
}

# In-process cache of classification data. Sector/asset-type/name are slow to
# change; refetching them on every request would hammer Yahoo for no benefit.
# (Prices were historically kept live per request here too — see the note at
# _price_cache above: they now get a short 5-minute TTL as an accepted tradeoff.)
_classification_cache: dict[str, dict] = {}


def init_db() -> None:
    """Create all tables mapped onto ``Base`` if they do not exist yet."""
    Base.metadata.create_all(engine)


def get_stock_info(symbol: str, session: Session | None = None) -> dict | None:
    """Return one symbol's stored classification row as a dict, or ``None``.

    The returned mapping has exactly the column names plus ``raw_info`` already
    JSON-decoded back to its original dict (or ``{}`` if it was stored empty/
    unparseable). This is the read-through source for
    :func:`get_symbol_classifications`: a non-None result here means NO live
    yfinance call may be made for that symbol.

    When no session is passed, one is opened and closed locally (and committed
    nothing — this is a pure read). Callers with an ambient session should pass
    it in so the read joins their transaction.
    """
    own_session = session is None
    if own_session:
        session = SessionLocal()
    try:
        row = session.get(StockInfo, symbol)
        if row is None:
            return None
        try:
            decoded_raw = json.loads(row.raw_info) if row.raw_info else {}
        except (TypeError, ValueError):
            logger.warning("stock_info.raw_info for %s was not valid JSON; using {}", symbol)
            decoded_raw = {}
        return {
            "name": row.name,
            "sector": row.sector,
            "asset_type": row.asset_type,
            "quote_type": row.quote_type,
            "currency": row.currency,
            "exchange": row.exchange,
            "raw_info": decoded_raw,
            "updated_at": row.updated_at,
        }
    finally:
        if own_session:
            session.close()


def upsert_stock_info(
    symbol: str,
    info_dict: dict | None = None,
    derived: dict | None = None,
    session: Session | None = None,
) -> None:
    """Upsert one symbol's classification into ``stock_info`` (keyed on PK).

    ``info_dict`` is the yfinance ``Ticker.info``-shaped mapping produced by
    :func:`_fetch_all_info`; it is stored verbatim (JSON-encoded) in
    ``raw_info`` so any fetched field without its own column survives
    round-tripping.

    ``derived`` optionally carries the classification result already computed
    here — keys ``sector`` and/or ``asset_type``, e.g.
    ``{"sector": "Technology", "asset_type": "Stock"}``. When present these win
    over re-deriving from raw fields, so the dedicated columns hold the *final*
    classification (SECTOR_MAP fallbacks included), not just whatever yfinance
    happened to return — that is what makes a later DB-only lookup reproduce
    the same answer with no live call.

    Re-calling for an existing symbol replaces (never duplicates) the row and
    refreshes ``updated_at``; calling it again with identical content is a safe
    no-op apart from that timestamp bump. Commits when given its own session.
    """
    info = dict(info_dict or {})
    derived = derived or {}

    def _first_str(*keys: str):
        for key in keys:
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    name = _first_str("shortName", "longName") or symbol
    sector = derived.get("sector")
    if not (isinstance(sector, str) and sector):
        raw_sector = info.get("sector")
        sector = raw_sector if isinstance(raw_sector, str) and raw_sector else None
    asset_type = derived.get("asset_type") or _first_str("instrumentType")

    own_session = session is None
    if own_session:
        session = SessionLocal()
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        stmt = sqlite_insert(StockInfo).values(
            symbol=symbol,
            name=name,
            sector=sector,
            asset_type=asset_type,
            quote_type=_first_str("quoteType"),
            currency=_first_str("currency"),
            exchange=_first_str("fullExchangeName") or _first_str("exchange"),
            raw_info=json.dumps(info),
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[StockInfo.symbol],
            values={
                "name": name,
                "sector": sector,
                "asset_type": asset_type,
                "quote_type": _first_str("quoteType"),
                "currency": _first_str("currency"),
                "exchange": _first_str("fullExchangeName") or _first_str("exchange"),
                "raw_info": json.dumps(info),
                "updated_at": now,
            },
        )
        session.execute(stmt)
        session.commit()
    finally:
        if own_session:
            session.close()


def _safe_info(ticker) -> dict:
    """Fetch ``Ticker.info`` tolerating any exception (empty dict on failure)."""
    try:
        return ticker.info or {}
    except Exception as exc:  # noqa: BLE001 - classification is best-effort per symbol
        logger.warning("yfinance info fetch failed for %s: %s", ticker.ticker, exc)
        return {}


def _fetch_all_info(symbols: list[str]) -> dict[str, dict]:
    """One batched yfinance call returning ``{symbol: info-dict}``.

    Uses the ``yf.Tickers`` batch (verified empirically to resolve every held
    symbol in a single request). Symbols that fail individually get an empty
    dict so callers can fall back per-symbol without crashing.
    """
    if not symbols:
        return {}
    deadline = datetime.now(timezone.utc) + timedelta(
        seconds=PRICE_FETCH_OVERALL_BUDGET_SECONDS
    )
    try:
        tickers = yf.Tickers(" ".join(symbols))
    except Exception as exc:  # noqa: BLE001 - batch construction is best-effort
        logger.warning("yfinance Tickers batch failed for %s: %s", symbols, exc)
        return {}

    infos: dict[str, dict] = {}
    for symbol in symbols:
        if datetime.now(timezone.utc) >= deadline:
            break
        info = _safe_info(tickers.tickers[symbol])
        infos[symbol] = info or {
            "shortName": symbol,  # keep a name even when nothing else resolved
        }
    return infos


def _cached_fetch_all_info(symbols: list[str]) -> dict[str, dict]:
    """Return ``_fetch_all_info``'s result, served from the shared price cache
    when an entry for this exact symbol set is still within its TTL.

    The key is order-independent (see :func:`_price_cache_key`), so any two of
    the 5 yfinance-consuming endpoints requesting overlapping symbol sets share
    one cached batch instead of each hitting ``yf.Tickers(...)`` live — that's
    what collapses a cold page load's ~75s fan-out into a single fetch. See
    todo_cache.md for the full rationale and scope boundary (this wrapper only,
    never :func:`_fetch_all_info` itself or :func:`get_symbol_classifications`).
    """
    key = _price_cache_key(symbols)
    cached = _price_cache.get(key)
    if cached is not None:
        return cached
    result = _fetch_all_info(symbols)
    _price_cache.put(key, result, ttl_seconds=DEFAULT_TTL)
    return result


def fetch_live_prices(
    symbols: list[str],
    fallback_prices: dict[str, float] | None = None,
) -> tuple[dict[str, float], str]:
    """Resolve a live/current price for every symbol in ``symbols``.

    Single batched yfinance call; price is taken from ``regularMarketPrice``,
    falling back to ``currentPrice`` within the same info dict (both verified
    present on real Yahoo responses). Any symbol that fails (network error,
    missing/zero price) falls back to its most recent transaction Price via
    ``fallback_prices[symbol]`` — logged with a warning naming the symbol — so
    portfolioValue is always computable.

    Returns ``(prices, priceAsOf)``: prices maps every requested symbol that
    could be valued to a float; priceAsOf is an ISO-8601 UTC timestamp of when
    the fetch ran, with ``' (stale fallback for N symbols)'`` appended if any
    symbol fell back.

    Note: classification fields are fetched in the same batch but this function
    only returns prices — callers wanting sector/asset-type/name should call
    :func:`get_symbol_classifications` instead (cached, so no extra round trip
    when both are needed for the same symbols).
    """
    infos = _cached_fetch_all_info(symbols)

    def _fallback(symbol: str, reason: str) -> float | None:
        value = (fallback_prices or {}).get(symbol)
        if value is not None and value > 0:
            logger.warning(
                "Live price unavailable for %s (%s); using last transaction price %.4f",
                symbol,
                reason,
                value,
            )
            return value
        return None

    prices: dict[str, float] = {}
    fallback_count = 0
    for symbol in symbols:
        info = infos.get(symbol) or {}
        live: float | None = None
        for key in ("regularMarketPrice", "currentPrice"):
            value = info.get(key)
            if isinstance(value, (int, float)) and value > 0:
                live = float(value)
                break

        if live is not None:
            prices[symbol] = live
            continue
        value = _fallback(symbol, "yfinance returned no regularMarketPrice/currentPrice")
        if value is not None:
            prices[symbol] = value
            fallback_count += 1
        else:
            logger.warning("No price resolvable for %s; excluding from valuation", symbol)

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if fallback_count:
        fetched_at += f" (stale fallback for {fallback_count} symbols)"
    return prices, fetched_at


def _classify_sector(info: dict[str, object], symbol: str) -> str:
    """Derive the sector bucket label for one symbol.

    Rule: ETFs always bucket as ``"ETF"``; equities use yfinance's live
    ``sector`` when present; on failure fall back to SECTOR_MAP, then to an
    instrumentType-based bucket ("ETF" if it looks like an ETF, else "Other"),
    logging a warning naming the symbol whenever the live value was missing.
    """
    quote_type = str(info.get("quoteType") or "").upper()
    sector = info.get("sector")
    category = info.get("category")

    if quote_type == "ETF" or (not sector and category):
        return "ETF"
    if isinstance(sector, str) and sector:
        return sector

    # yfinance gave us nothing usable — fall back to the documented map.
    mapped = SECTOR_MAP.get(symbol)
    logger.warning(
        "No live sector from yfinance for %s (quoteType=%r category=%r); using fallback",
        symbol,
        quote_type or None,
        category if not isinstance(sector, str) else None,
    )
    if mapped:
        return mapped

    instrument = str(info.get("instrumentType") or "").upper()
    bucket = "ETF" if ("ETF" in instrument or "FUND" in instrument) else "Other"
    logger.warning(
        "%s not in SECTOR_MAP either; defaulting to %r bucket", symbol, bucket
    )
    return bucket


def _classification_from_stored_info(symbol: str, stored: dict) -> dict:
    """Rebuild the public classification entry from one ``stock_info`` row.

    The dedicated columns hold the *final* classification (already including
    any SECTOR_MAP fallback applied at fetch time), so they are used directly.
    If a column is empty — e.g. the symbol was harvested before this code and
    its live data never resolved — re-derive it from the stored ``raw_info``
    using exactly the same rules as the live path (SECTOR_MAP included, keyed
    on ``symbol``), so DB-only answers match what a live call would have
    produced.
    """
    raw = stored.get("raw_info") or {}
    sector = stored.get("sector")
    if not (isinstance(sector, str) and sector):
        sector = _classify_sector(raw, symbol)  # same rules as the live path

    instrument = str(raw.get("instrumentType") or "").upper()
    quote_type = str(stored.get("quote_type") or raw.get("quoteType") or "").upper()
    asset_type = stored.get("asset_type")
    if not (isinstance(asset_type, str) and asset_type):
        asset_type = "ETF" if ("ETF" in instrument or quote_type == "ETF") else "Stock"

    name = stored.get("name") or raw.get("shortName") or raw.get("longName") or symbol
    return {
        "sector": sector,
        "assetType": asset_type,
        "name": str(name),
    }


def get_symbol_classifications(symbols: list[str]) -> dict[str, dict]:
    """Classify each held symbol for the sector/holdings endpoints.

    Returns ``{symbol: {"sector": str, "assetType": str, "name": str}}`` where:
      - ``sector`` follows :func:`_classify_sector` (live yfinance data first);
      - ``assetType`` is live ``instrumentType`` mapped to "ETF" or "Stock";
      - ``name`` prefers Yahoo's shortName, then longName, then the symbol.

    Resolution order per symbol:
      1. in-process memo — repeated requests within one server run skip even
         the DB read;
      2. the ``stock_info`` table — a row here means NO live yfinance call is
         made, on this or any future process (the stored columns hold the final
         classification, re-derived from stored ``raw_info`` only when empty);
      3. a live batched yfinance info fetch — used only for symbols missing
         from both layers above; its result is upserted into ``stock_info`` so
         every later lookup (even in a fresh process) stays DB-only.

    Symbols that resolve to nothing at all still get a best-effort entry
    (sector "Other", assetType "Stock", name = symbol) so downstream grouping
    code never has to special-case them; such symbols are NOT persisted, so the
    next request retries live data for them.
    """
    missing = [s for s in symbols if s not in _classification_cache]

    db_sourced: dict[str, dict] = {}
    live_needed: list[str] = []
    for symbol in missing:
        try:
            stored = get_stock_info(symbol)
        except Exception as exc:  # noqa: BLE001 - DB hiccups must not break classification
            logger.warning("stock_info lookup failed for %s; falling back to live yfinance: %s", symbol, exc)
            stored = None
        if stored is not None:
            db_sourced[symbol] = stored
        else:
            live_needed.append(symbol)

    infos = _cached_fetch_all_info(live_needed) if live_needed else {}

    for symbol in missing:
        if symbol in db_sourced:
            entry = _classification_from_stored_info(symbol, db_sourced[symbol])
        else:
            info = infos.get(symbol) or {}
            instrument = str(info.get("instrumentType") or "").upper()
            quote_type = str(info.get("quoteType") or "").upper()
            asset_type = "ETF" if ("ETF" in instrument or quote_type == "ETF") else "Stock"
            name = info.get("shortName") or info.get("longName") or symbol
            sector = _classify_sector(info, symbol)

            resolved = bool(info) and (sector != "Other" or symbol not in SECTOR_MAP)
            if resolved:
                try:
                    # Persist the final classification so every later lookup —
                    # including in a brand-new process with an empty in-memory
                    # cache — is DB-only.
                    upsert_stock_info(
                        symbol,
                        info,
                        derived={"sector": sector, "asset_type": asset_type},
                    )
                except Exception as exc:  # noqa: BLE001 - persistence is best-effort
                    logger.warning("Failed to persist stock_info for %s: %s", symbol, exc)

            entry = {"sector": sector, "assetType": asset_type, "name": str(name)}

        _classification_cache[symbol] = entry

    return {symbol: _classification_cache[symbol] for symbol in symbols}


def last_transaction_prices(symbols: list[str]) -> dict[str, float]:
    """Most recent non-zero transaction Price per symbol from `transactions`.

    This is the ``fallback_prices`` argument :func:`fetch_live_prices` expects.
    "Most recent" means the latest activity_date (ISO dates sort lexically); a
    symbol's rows are scanned newest-first and its first parseable positive
    price wins. Symbols with no usable row are simply absent from the result,
    which makes fetch_live_prices drop them from valuation entirely.
    """
    if not symbols:
        return {}

    from app.models import Transaction  # local import avoids a cycle at module load

    fallbacks: dict[str, float] = {}
    session = SessionLocal()
    try:
        rows = (
            session.query(Transaction)
            .filter(Transaction.symbol.in_(symbols))
            .order_by(Transaction.activity_date.desc())
            .all()
        )
        for row in rows:  # already newest-first per symbol thanks to global sort
            if row.symbol in fallbacks or not row.symbol:
                continue
            price = _parse_money(row.price)
            if price is None:
                continue
            if price > 0:
                fallbacks[row.symbol] = price
    finally:
        session.close()
    return fallbacks


def _parse_money(value: str | None) -> float | None:
    """Parse a money string like ``'$34.16'`` / ``'( $1,200.50 )'`` to a float.

    Handles the formatted values stored in the all-TEXT ``transactions`` columns
    (leading `$`, thousands commas, parentheses for negatives) exactly as
    StockMcpServer's ``parse_accounting_number`` does; returns ``None`` when the
    value is empty or unparseable so callers can skip it rather than crash.
    """
    if not value:
        return None
    text = str(value).strip()
    negative = text.startswith("(") and text.endswith(")")
    text = (
        text.replace("$", "")
        .replace(",", "")
        .replace("(", "")
        .replace(")", "")
        .strip()
    )
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number
