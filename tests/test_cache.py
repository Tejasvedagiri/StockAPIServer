"""TTL-cache behavior tests for app/cache.py's TTLCache as consumed by
app.market_data._cached_fetch_all_info (the yfinance price-fetch layer).

Two behaviors under test here:

1. TTL expiry re-trigger -- a second call within the window is served from
   cache WITHOUT re-calling the fetcher; after the TTL elapses, a call
   re-triggers the live fetch exactly once. Uses an injected sub-second TTL
   (patching ``app.market_data.DEFAULT_TTL``) instead of waiting out the 5-
   minute production default.

2. LRU eviction -- with a small bounded max size and short TTLs that keep
   entries valid, filling past capacity evicts the least-recently-used entry;
   requesting it again re-computes while recent entries stay cached.

No real Yahoo / network in either: the fetcher is monkeypatched at module level
(``app.market_data._fetch_all_info``), so call counts are exact and no yfinance
import side effects leak into timing. Follows this repo's plain sync test style
(no pytest-asyncio).
"""

import time

from app import market_data


class TestTtlExpiryReTrigger:
    def _run(self, monkeypatch):
        # Inject a sub-second TTL at the point where the wrapper stores results
        # (market_data does `_price_cache.put(key, result, ttl_seconds=DEFAULT_TTL)`),
        # and keep app.cache's own default in step so nothing else lags behind.
        import app.cache as cache_module

        monkeypatch.setattr(market_data, "DEFAULT_TTL", 0.2)
        monkeypatch.setattr(cache_module, "DEFAULT_TTL", 0.2)

        calls = []

        def fake_fetch_all_info(symbols):
            calls.append(list(symbols))
            return {s: {"shortName": f"{s} Corp"} for s in symbols}

        monkeypatch.setattr(market_data, "_fetch_all_info", fake_fetch_all_info)
        market_data._price_cache.clear()

        # First call: cold miss -> exactly one live fetch.
        first = market_data._cached_fetch_all_info(["AAPL", "MSFT"])
        assert len(calls) == 1
        assert set(first.keys()) == {"AAPL", "MSFT"}

        # Second call within the window: served from cache, NO re-fetch. The
        # key is order-independent (sorted symbol tuple), so reversed input
        # still hits the same entry.
        second = market_data._cached_fetch_all_info(["MSFT", "AAPL"])
        assert len(calls) == 1, "second call within TTL must not re-fetch"
        assert second == first

        # After expiry: a call re-triggers the live fetch exactly once.
        time.sleep(0.25)
        third = market_data._cached_fetch_all_info(["AAPL", "MSFT"])
        assert len(calls) == 2, (
            f"expected exactly one re-fetch after TTL expiry, got {len(calls)} total calls"
        )
        assert third == first

    def test_second_call_within_ttl_is_cached_then_expiry_refetches_once(self, monkeypatch):
        try:
            self._run(monkeypatch)
        finally:
            market_data._price_cache.clear()


class TestLruEviction:
    def _run(self, monkeypatch):
        import app.cache as cache_module

        # Comfortable TTL so every entry stays valid while we work -- eviction
        # under test is size-driven, not time-driven.
        monkeypatch.setattr(market_data, "DEFAULT_TTL", 30)
        monkeypatch.setattr(cache_module, "DEFAULT_TTL", 30)

        # Shrink to a small bound for the duration of this test (TTLCache has no
        # resize API; swap in a fresh bounded one).
        market_data._price_cache = cache_module.TTLCache(max_size=3)

        calls = []

        def fake_fetch_all_info(symbols):
            calls.append(list(symbols))
            return {s: {"shortName": f"{s} Corp"} for s in symbols}

        monkeypatch.setattr(market_data, "_fetch_all_info", fake_fetch_all_info)

        # Fill to capacity (3 slots): A, B, C -- all valid.
        a1 = market_data._cached_fetch_all_info(["AAA"])  # miss -> [A]
        b1 = market_data._cached_fetch_all_info(["BBB"])  # miss -> [A,B]
        c1 = market_data._cached_fetch_all_info(["CCC"])  # miss -> [A,B,C] (full)
        assert len(calls) == 3

        # Touch A so it becomes most-recent; now inserting D must evict B -- the
        # least-recently-used entry, NOT the oldest-inserted one (A).
        a2 = market_data._cached_fetch_all_info(["AAA"])  # hit -> [B,C,A]
        assert len(calls) == 3, "re-touch of AAA within TTL must be a cache hit"
        d1 = market_data._cached_fetch_all_info(["DDD"])  # miss -> evicts B -> [C,A,D]

        assert len(calls) == 4

        # B was the LRU victim: requesting it re-computes (and that recompute's
        # put in turn evicts C, the new LRU -- expected and irrelevant to what
        # we're asserting next).
        b2 = market_data._cached_fetch_all_info(["BBB"])  # miss -> [A,D,B]
        assert len(calls) == 5, "evicted entry must be recomputed on next request"
        assert b2 == b1

        # A recent (non-evicted-at-insert-time) entry is still served from cache:
        # requesting D triggers no new fetch.
        d2 = market_data._cached_fetch_all_info(["DDD"])  # hit -> [A,B,D]
        assert len(calls) == 5, "recent entries must remain cached after eviction"

    def test_oldest_entry_evicted_when_capacity_exceeded(self, monkeypatch):
        try:
            self._run(monkeypatch)
        finally:
            market_data._price_cache.clear()
