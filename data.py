"""Data layer for the stock API: thin wrappers around yfinance.

Three logical "tables", all fetched on demand (no local DB):

- get_live_prices(symbols) -> latest price per symbol
- get_stock_info(symbols)  -> static info fields per symbol
- get_ohlc(symbols, timerange, interval) -> OHLC rows for a time range

Every function returns plain JSON-safe Python values (lists of dicts /
scalars coerced to str/float/int/None so FastAPI can serialize them).
"""

import yfinance as yf

# Used whenever the caller does not pass an explicit symbol list.
DEFAULT_SYMBOLS = ["AAPL", "MSFT"]


def _json_safe(value):
    """Coerce a single value to something JSON-serializable."""
    if value is None:
        return None
    # pandas NA / NaT and numpy scalars come through here from fast_info/info.
    try:
        import math

        if isinstance(value, float) or hasattr(value, "item"):
            f = float(value)
            return None if (math.isnan(f)) else f
        if hasattr(value, "isoformat"):  # Timestamp / datetime / date
            return value.isoformat()
    except Exception:
        pass
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item"):  # remaining numpy scalars (int64 etc.)
        try:
            return value.item()
        except Exception:
            pass
    return value


def _clean(obj):
    """Deep-coerce a nested structure of dicts/lists to JSON-safe values."""
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return _json_safe(obj)


def get_live_prices(symbols):
    """Latest price for each symbol: [{'symbol', 'price', 'as_of'}, ...].

    A symbol whose fetch raises is not fatal — it comes back as
    {'symbol': sym, 'error': str(e)} instead.
    """
    rows = []
    for sym in symbols:
        try:
            rows.append(_live_price_row(sym))
        except Exception as e:  # per-symbol containment, never raise out
            rows.append({"symbol": sym, "price": None, "as_of": None, "error": str(e)})
    return _clean(rows)


def _live_price_row(sym):
    t = yf.Ticker(sym)
    fi = t.fast_info
    try:
        price = float(fi["lastPrice"]) if "lastPrice" in fi else None
    except (TypeError, ValueError, KeyError):
        price = None
    as_of = None
    if price is None or price == 0:
        # Fallback: last row of a short history call.
        hist = t.history(period="1d", interval="1m")
        if not hist.empty:
            last = hist.iloc[-1]
            price = float(last["Close"])
            as_of = str(hist.index[-1])
    return {
        "symbol": sym,
        "price": _json_safe(price),
        "as_of": as_of or _now_iso(),
    }


def _now_iso():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def get_stock_info(symbols):
    """Static info fields for each symbol: [{'symbol', 'name', ...}, ...]."""
    rows = []
    for sym in symbols:
        try:
            rows.append(_stock_info_row(sym))
        except Exception as e:  # per-symbol containment, never raise out
            rows.append({"symbol": sym, "error": str(e)})
    return _clean(rows)


def _stock_info_row(sym):
    t = yf.Ticker(sym)
    info = t.info or {}

    def pick(key):
        v = info.get(key)
        if isinstance(v, (list, tuple)):
            return ", ".join(str(x) for x in v[:10])
        return _json_safe(v)

    return {
        "symbol": sym,
        "name": pick("longName") or pick("shortName"),
        "sector": pick("sector"),
        "industry": pick("industry"),
        "marketCap": pick("marketCap"),
        "currency": pick("currency"),
        "exchange": pick("fullExchangeName") or pick("exchange"),
    }


def get_ohlc(symbols, timerange="5d", interval="1d"):
    """OHLC rows for a time range: [{'symbol', 'date', 'open', 'high', 'low', 'close', 'volume'}, ...].

    Rows from all symbols are flattened into one list sorted by date.
    A bogus timerange/interval (yfinance yields no rows) results in an
    empty list, never a raise; per-symbol fetch errors are skipped the
    same way as in get_live_prices/get_stock_info.
    """
    rows = []
    for sym in symbols:
        try:
            hist = yf.Ticker(sym).history(period=timerange, interval=interval)
        except Exception:  # per-symbol containment, never raise out
            continue
        if hist is None or hist.empty:
            continue
        for idx, row in hist.iterrows():
            rows.append({
                "symbol": sym,
                "date": _json_safe(idx),
                "open": _json_safe(row["Open"]),
                "high": _json_safe(row["High"]),
                "low": _json_safe(row["Low"]),
                "close": _json_safe(row["Close"]),
                "volume": _json_safe(row.get("Volume")),
            })
    rows.sort(key=lambda r: (r["date"] or ""))
    return _clean(rows)
