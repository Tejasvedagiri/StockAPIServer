"""Small hand-rolled TTL + LRU cache (stdlib only: time.monotonic + OrderedDict).

Two independent in-memory layers use this module (per todo_cache.md):

1. Endpoint-response caching — one shared :class:`TTLCache` instance, keyed by
   endpoint name only (none of the 9 cached ``GET *.json`` endpoints take query
   params), so a burst of requests within the TTL window is served from memory
   instead of re-running the full pipeline per request.

2. yfinance price-fetch caching — a second shared :class:`TTLCache` instance,
   keyed by a sorted symbol tuple (see ``app.market_data._cached_fetch_all_info``),
   bounding how often ``yf.Tickers(...).info`` is hit for overlapping symbol sets.

Both are intentionally in-memory only: no persistence across process restarts.
The TTL duration is injectable per call, so unit tests can use a sub-second TTL
instead of waiting out the 5-minute (300 s) production default.
"""

import copy
import functools
import time
from collections import OrderedDict

DEFAULT_TTL = 300


class TTLCache:
    """Bounded LRU cache with per-entry monotonic-clock expiry."""

    def __init__(self, max_size: int):
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self.max_size = max_size
        # key -> (value, deadline). Iteration order is recency — the first item
        # is the least-recently-used one and the eviction victim when full.
        self._entries: "OrderedDict[object, tuple]" = OrderedDict()

    def _purge_expired(self) -> None:
        """Drop every entry whose monotonic deadline has passed.

        Called before any size accounting so expired values are never served
        (``get`` also re-checks its own entry) and never count toward the
        max-size bound when deciding whether to evict live entries.
        """
        now = time.monotonic()
        for key in [k for k, (_, deadline) in self._entries.items() if deadline <= now]:
            del self._entries[key]

    def get(self, key):
        """Return the cached value only while unexpired; else delete + None.

        A hit also moves the entry to the most-recent position so reads count
        toward LRU recency. Note: a legitimately stored ``None`` is indistinct
        from a miss by design (both consuming layers store non-None payloads).
        """
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, deadline = entry
        if deadline <= time.monotonic():
            del self._entries[key]  # expired — never serve it
            return None
        self._entries.move_to_end(key)  # hit refreshes LRU recency
        return value

    def put(self, key, value, ttl_seconds=DEFAULT_TTL) -> None:
        """Store ``value`` with a monotonic deadline of now + ttl_seconds.

        Overwriting an existing key refreshes both its deadline and its
        recency (moved to most-recent). When the cache exceeds max_size after
        an insert, the least-recently-used entries are evicted until within
        bound — expired entries are purged first so they don't displace live
        ones.
        """
        self._purge_expired()
        deadline = time.monotonic() + ttl_seconds
        if key in self._entries:
            # Overwrite: keep the existing slot, refresh value/deadline/recency.
            self._entries[key] = (value, deadline)
            self._entries.move_to_end(key)
        else:
            self._entries[key] = (value, deadline)
            while len(self._entries) > self.max_size:
                # Evict the least-recently-used entry.
                self._entries.popitem(last=False)

    def clear(self) -> None:
        """Drop everything (used by tests' autouse reset fixture)."""
        self._entries.clear()


def get_or_compute(cache: TTLCache, key, fn, ttl=None):
    """Return ``cache.get(key)`` on a hit; otherwise compute and store.

    On a miss (or an expired/absent entry), calls ``fn()``, stores the result
    via :meth:`TTLCache.put` under the same key — with ``ttl`` if given, else
    the module-level :data:`DEFAULT_TTL` — and returns that fresh value. This
    is the single entry point both consuming layers wrap their expensive calls
    through: the endpoint-response cache (keyed by endpoint name) and the
    yfinance price-fetch wrapper in ``app.market_data`` (keyed by a sorted
    symbol tuple).
    """
    cached = cache.get(key)
    if cached is not None:
        return cached
    value = fn()
    effective_ttl = ttl if ttl is not None else DEFAULT_TTL
    cache.put(key, value, ttl_seconds=effective_ttl)
    return value


# ---------------------------------------------------------------------------
# Endpoint-response layer (layer 1 of the two this module serves).
#
# One shared instance for all 9 cached ``GET *.json`` route handlers — keyed
# by endpoint name only (none of them take query params, confirmed in
# todo_cache.md). There are exactly 9 possible keys; max_size=16 leaves room
# to spare without any real memory cost.
# ---------------------------------------------------------------------------

_endpoint_cache = TTLCache(max_size=16)


def cached_response(endpoint_name: str):
    """Decorator: serve a FastAPI route handler's result from the shared
    endpoint-response cache for :data:`DEFAULT_TTL` seconds (5 minutes).

    Key is ``endpoint_name`` ONLY — no query params, path args or headers are
    part of it. That matches the 9 handlers this wraps, none of which accept
    any parameters; pass a distinct name per route when wiring it up. On a
    miss/expired entry the real handler runs and its return value is stored;
    within the window subsequent requests skip the full pipeline entirely.

    In-memory only — nothing survives a process restart (a fresh request after
    a restart re-runs the handler for real, by design). The TTL duration here
    uses the module-level :data:`DEFAULT_TTL`; unit tests that want sub-second
    windows can call :class:`TTLCache` / :func:`get_or_compute` directly with
    an explicit ``ttl`` instead of waiting on this decorator.
    """

    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(*args, **kwargs):
            cached = _endpoint_cache.get(endpoint_name)
            if cached is not None:
                # Hit: hand back a fresh deep copy so no two callers of the same
                # cached entry can mutate each other's data in place (the stored
                # payload itself stays pristine until it expires or is evicted).
                return copy.deepcopy(cached)
            value = await handler(*args, **kwargs)
            # FastAPI handlers here always return non-None payloads (a list or
            # a dict of JSON-shaped data); storing None would make get() treat
            # the next request as a miss, so guard it explicitly.
            if value is not None:
                _endpoint_cache.put(endpoint_name, value)
            return value

        return wrapper

    return decorator


def reset_endpoint_cache() -> None:
    """Drop all cached endpoint responses (test helper; see the autouse fixture)."""
    _endpoint_cache.clear()
