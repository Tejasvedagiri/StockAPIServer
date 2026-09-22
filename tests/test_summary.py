"""Shape/type tests for GET /summary.json against a temporary SQLite DB.

Mirrors the conftest pattern of tests/test_harvest_csv_endpoint.py: build a
throwaway SQLite DB with just the `transactions` table, point the app's
session factory at it, and mock the two market_data functions so no live
Yahoo Finance call happens during the test run (deterministic prices +
priceAsOf).

Assertions cover API.md's /summary.json contract:
* exact top-level field set (14 keys),
* every numeric field is a JSON number — dollars 2dp, *Pct 1dp, never strings,
* priceAsOf carries the documented Z-suffix shape and the stale-fallback note.
"""

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import db, market_data, main
from app.models import Transaction

#: API.md's documented /summary.json top-level field set — no extras allowed.
EXPECTED_FIELDS = {
    "portfolioValue",
    "costBasis",
    "unrealizedGainLoss",
    "unrealizedGainLossPct",
    "realizedGainLoss",
    "dividendsTotal",
    "gainLossInclDividends",
    "gainLossInclDividendsPct",
    "cashAvailable",
    "totalDeposited",
    "holdingsCount",
    "allocation",
    "winLoss",
    "priceAsOf",
}


@pytest.fixture()
def temp_summary_env(tmp_path, monkeypatch):
    """Temp DB with one AAPL buy (2 @ $50) + one ACH deposit ($50)."""
    db_file = tmp_path / "summary_test.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Transaction.__table__.create(engine)
    session_factory = sessionmaker(bind=engine)

    session = session_factory()
    try:
        session.add_all([
            Transaction(
                activity_date="01/05/2024", process_date="01/05/2024", settle_date="01/09/2024",
                symbol="AAPL", description="Buy AAPL 2 @ $50.00", trans_code="Buy",
                quantity="2.000000", price="$50.00", amount="-$100.00",
            ),
            Transaction(
                activity_date="01/06/2024", process_date="01/06/2024", settle_date="01/10/2024",
                symbol="", description="Direct Deposit from ACME CORP", trans_code="ACH",
                quantity="", price="", amount="$50.00",
            ),
        ])
        session.commit()
    finally:
        session.close()

    # holdings._load_transaction_rows imports SessionLocal from app.db at call
    # time; main.py imported the name directly — patch both references.
    monkeypatch.setattr(db, "SessionLocal", session_factory)
    monkeypatch.setattr(main, "SessionLocal", session_factory)

    yield {"session_factory": session_factory}


@pytest.fixture()
def client(temp_summary_env):
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture()
def live_prices_mock(monkeypatch):
    """Deterministic market data: AAPL at $60, fresh timestamp (no stale note)."""

    def fake_last_transaction_prices(symbols):
        return {sym: 50.0 for sym in symbols if sym == "AAPL"}

    def fake_fetch_live_prices(symbols, fallback_prices=None):
        return ({sym: 60.0 for sym in symbols if sym == "AAPL"}, "2026-05-31T14:02:11+00:00")

    monkeypatch.setattr(market_data, "last_transaction_prices", fake_last_transaction_prices)
    monkeypatch.setattr(market_data, "fetch_live_prices", fake_fetch_live_prices)


def test_field_set_matches_api_md_exactly(client, live_prices_mock):
    body = client.get("/summary.json").json()
    assert set(body.keys()) == EXPECTED_FIELDS
    assert set(body["allocation"].keys()) == {"stocksPct", "cashPct"}
    assert set(body["winLoss"].keys()) == {"totalWin", "totalLoss", "winRatePct"}


def test_numeric_fields_are_json_numbers_with_correct_rounding(client, live_prices_mock):
    """Every numeric field must be a JSON number (not a string) — the classic
    /summary.json vs /transactions.json gotcha — with dollars at 2dp and *Pct
    at 1dp."""
    response = client.get("/summary.json")
    body = response.json()

    # Types: numbers, never strings.
    for key in EXPECTED_FIELDS - {"allocation", "winLoss", "priceAsOf"}:
        assert isinstance(body[key], (int, float)) and not isinstance(body[key], bool), \
            f"{key} must be a JSON number, got {type(body[key]).__name__}: {body[key]!r}"
    for key in ("stocksPct", "cashPct"):
        assert isinstance(body["allocation"][key], (int, float))
    for key in ("totalWin", "totalLoss", "winRatePct"):
        assert isinstance(body["winLoss"][key], (int, float))
    assert isinstance(body["priceAsOf"], str)

    # Rounding: no number on the wire may carry more than 2 decimal digits.
    for match in re.finditer(r"-?\d+\.\d+", response.text):
        decimals = len(match.group(0).rsplit(".", 1)[1])
        assert decimals <= 2, f"number with >2 decimal digits on the wire: {match.group(0)}"


def test_expected_kpi_values_for_known_fixture(client, live_prices_mock):
    """AAPL: bought 2 @ $50 (cost basis $100), now priced at $60; ACH deposit $50.

    portfolioValue = 2 x 60 = 120.00; unrealized +20.00 (+20%);
    cashAvailable = 50 - 100 = -50.00; totalDeposited = 50.00.
    """
    body = client.get("/summary.json").json()

    assert body["portfolioValue"] == 120.0
    assert body["costBasis"] == 100.0
    assert body["unrealizedGainLoss"] == 20.0
    assert body["unrealizedGainLossPct"] == 20.0
    assert body["realizedGainLoss"] == 0.0
    assert body["dividendsTotal"] == 0.0
    assert body["gainLossInclDividends"] == 20.0
    assert body["gainLossInclDividendsPct"] == 20.0
    assert body["cashAvailable"] == -50.0
    assert body["totalDeposited"] == 50.0
    assert body["holdingsCount"] == 1

    # allocation: total account value = 120 + (-50) = 70 -> stocksPct = 171.4
    assert body["allocation"]["stocksPct"] == pytest.approx(171.4, abs=0.05)
    assert body["allocation"]["cashPct"] == round(100 - body["allocation"]["stocksPct"], 1)

    # No sells in the fixture: win-loss all zero, no rate.
    assert body["winLoss"]["totalWin"] == 0.0
    assert body["winLoss"]["totalLoss"] == 0.0
    assert body["winLoss"]["winRatePct"] == 0.0


def test_price_as_of_uses_z_suffix_and_no_stale_note_when_live(client, live_prices_mock):
    body = client.get("/summary.json").json()
    assert body["priceAsOf"].endswith("Z")
    assert "+00:00" not in body["priceAsOf"]
    assert "stale" not in body["priceAsOf"]


def test_price_as_of_carries_stale_note_when_fallback_occurs(client, monkeypatch):
    monkeypatch.setattr(
        market_data, "last_transaction_prices", lambda symbols: {sym: 50.0 for sym in symbols}
    )
    monkeypatch.setattr(
        market_data,
        "fetch_live_prices",
        lambda symbols, fallback_prices=None: (
            {"AAPL": 50.0},
            "2026-01-01T08:30:00+00:00 (stale fallback for 1 symbols)",
        ),
    )

    body = client.get("/summary.json").json()
    assert "(stale fallback for 1 symbols)" in body["priceAsOf"]
    # The +00:00 timestamp portion must still be normalized to Z form.
    assert "2026-01-01T08:30:00Z" in body["priceAsOf"]


def test_empty_transactions_table_returns_zeroed_shape(client, live_prices_mock):
    """No rows at all -> 200 with the same field set and zeroed KPIs."""
    session = db.SessionLocal()
    try:
        session.execute(text("DELETE FROM transactions"))
        session.commit()
    finally:
        session.close()

    body = client.get("/summary.json").json()
    assert set(body.keys()) == EXPECTED_FIELDS
    assert body["holdingsCount"] == 0
    assert body["portfolioValue"] == 0.0
    assert body["costBasis"] == 0.0
    assert body["unrealizedGainLossPct"] == 0.0
    assert body["gainLossInclDividendsPct"] == 0.0
    assert isinstance(body["priceAsOf"], str) and body["priceAsOf"].endswith("Z")
