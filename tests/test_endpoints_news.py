"""TestClient test for GET /newsItems.json (3.6.9), with the RSS fetcher
(app.routes.news.fetch_feed -- the one function that actually touches the
network) monkeypatched to return fixed headline data, so this asserts
shaping/typing without any network flakiness.
"""

from datetime import datetime, timedelta, timezone

from app.routes import news


def _fake_fetch_feed(symbol):
    now = datetime.now(timezone.utc)
    if symbol == "AAPL":
        return [
            {
                "id": "guid-1",
                "headline": "Apple posts record quarter",
                "published_at": now - timedelta(hours=2),
                "snippet": "Growth in subscriptions and hardware demand this period.",
            },
        ]
    if symbol == "MSFT":
        return [
            {
                "id": "guid-2",
                "headline": "Microsoft and AAPL both rally on cloud demand",
                "published_at": now - timedelta(hours=30),
                "snippet": "Cloud infrastructure spending continues to climb.",
            },
        ]
    return []


class TestNewsItemsJson:
    def test_shape_matches_api_md(self, client, monkeypatch):
        monkeypatch.setattr(news, "fetch_feed", _fake_fetch_feed)

        rows = client.get("/newsItems.json").json()
        assert rows
        for row in rows:
            assert set(row.keys()) == {
                "badge", "color", "headline", "snippet", "src", "time", "hours", "tickers",
            }
            assert isinstance(row["hours"], (int, float))
            assert isinstance(row["tickers"], list)
            assert all(isinstance(t, str) for t in row["tickers"])
            assert row["color"] in ("a", "b", "c", "d")

    def test_hours_is_a_real_number_not_a_string(self, client, monkeypatch):
        monkeypatch.setattr(news, "fetch_feed", _fake_fetch_feed)

        rows = client.get("/newsItems.json").json()
        aapl_item = next(r for r in rows if "Apple posts record" in r["headline"])
        assert aapl_item["hours"] == 2

    def test_tickers_includes_the_source_symbol_and_any_other_held_symbol_mentioned(self, client, monkeypatch):
        monkeypatch.setattr(news, "fetch_feed", _fake_fetch_feed)

        rows = client.get("/newsItems.json").json()
        msft_item = next(r for r in rows if "Microsoft and AAPL" in r["headline"])
        # The item came from MSFT's own feed but its text mentions AAPL too --
        # both held symbols must be tagged.
        assert msft_item["tickers"][0] == "MSFT"
        assert "AAPL" in msft_item["tickers"]

    def test_empty_ledger_degrades_to_empty_list_not_an_error(self, client, monkeypatch):
        from sqlalchemy import text

        from app import db

        monkeypatch.setattr(news, "fetch_feed", _fake_fetch_feed)
        session = db.SessionLocal()
        try:
            session.execute(text("DELETE FROM transactions"))
            session.commit()
        finally:
            session.close()

        response = client.get("/newsItems.json")
        assert response.status_code == 200
        assert response.json() == []

    def test_network_failure_for_one_symbol_never_500s(self, client, monkeypatch):
        """held_symbols() includes both AAPL and MSFT -- if one symbol's feed
        raises (network outage, malformed XML), the whole endpoint must still
        degrade gracefully rather than 500ing on an unrelated symbol's news."""

        def _flaky_fetch(symbol):
            if symbol == "AAPL":
                raise TimeoutError("simulated network outage")
            return _fake_fetch_feed(symbol)

        monkeypatch.setattr(news, "fetch_feed", _flaky_fetch)

        response = client.get("/newsItems.json")
        assert response.status_code == 200
        rows = response.json()
        assert all(isinstance(r, dict) for r in rows)
