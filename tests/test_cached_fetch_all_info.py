"""Focused unit test for app.market_data._cached_fetch_all_info in isolation:
the wrapper must (a) call the real _fetch_all_info exactly once on a cold
cache, returning its dict[str, dict] result unchanged (NOT a tuple -- the
(prices_dict, priceAsOf) tuple is fetch_live_prices' job), and (b) serve any
later identical-or-reordered request within the TTL from cache without
re-calling _fetch_all_info.

_fetch_all_info itself is monkeypatched at module level in app.market_data, so
no real yfinance / network call happens anywhere in this test.
"""

import pytest

from app import market_data


@pytest.fixture()
def cold_price_cache():
    """Start from an empty price cache regardless of what other tests left behind."""
    market_data._price_cache.clear()
    yield
    market_data._price_cache.clear()


class TestCachedFetchAllInfo:
    def test_first_call_fetches_once_and_returns_dict_not_tuple(
        self, cold_price_cache, monkeypatch
    ):
        calls = []

        def fake_fetch(symbols):
            calls.append(list(symbols))
            return {s: {"shortName": s} for s in symbols}

        monkeypatch.setattr(market_data, "_fetch_all_info", fake_fetch)

        result = market_data._cached_fetch_all_info(["AAPL", "MSFT"])

        assert len(calls) == 1, "cold cache must trigger exactly one live fetch"
        # Regression check: the wrapper's return type is dict[str, dict], not a
        # tuple -- wrapping it in (prices_dict, priceAsOf) belongs to
        # fetch_live_prices, which extracts those from this dict.
        assert isinstance(result, dict), f"expected dict, got {type(result).__name__}"
        assert set(result.keys()) == {"AAPL", "MSFT"}

    def test_second_identical_call_within_ttl_served_from_cache(
        self, cold_price_cache, monkeypatch
    ):
        calls = []

        def fake_fetch(symbols):
            calls.append(list(symbols))
            return {s: {"shortName": s} for s in symbols}

        monkeypatch.setattr(market_data, "_fetch_all_info", fake_fetch)

        first = market_data._cached_fetch_all_info(["AAPL", "MSFT"])
        second = market_data._cached_fetch_all_info(["MSFT", "AAPL"])  # reordered -> same key

        assert len(calls) == 1, "second call within TTL must be served from cache"
        assert second == first

    def test_result_stored_under_sorted_key(self, cold_price_cache, monkeypatch):
        monkeypatch.setattr(
            market_data, "_fetch_all_info", lambda symbols: {s: {} for s in symbols}
        )
        result = market_data._cached_fetch_all_info(["AAPL", "MSFT"])

        stored = market_data._price_cache.get(market_data._price_cache_key(["AAPL", "MSFT"]))
        assert stored == result, "miss path must store the fresh dict under the sorted key"
