"""Shared temp-DB-per-test fixtures, mirroring StockMcpServer's tests/conftest.py
approach and the pattern already established in tests/test_summary.py: a fresh
tmp-path SQLite file with just the `transactions` (+ `stock_info`) tables,
engine/session fixtures patched into app.db/app.main's SessionLocal, seeded
from small representative fixture rows -- so no test depends on
data/stockmcp.db or on live network.
"""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import cache as _app_cache, db, main, market_data
from app.models import StockInfo, Transaction


def _days_ago(n: int) -> str:
    """'M/D/YYYY' (no leading zeros, matching real Robinhood CSV style) for
    a date N days before today. Dates are relative to "today" rather than
    hardcoded, because app/routes/outlook.py's trailing-12-month dividend
    yield is computed against date.today() -- a hardcoded past date would
    silently fall outside that window depending on when tests happen to run
    (this bit the outlook endpoint tests once already: a hardcoded 2024
    dividend date aged out of the TTM window, making on/off look identical
    for a reason having nothing to do with the code under test)."""
    d = date.today() - timedelta(days=n)
    return f"{d.month}/{d.day}/{d.year}"


def _display_date(n: int) -> str:
    """format_activity_date's own 'Mon D, YYYY' rendering of _days_ago(n),
    for tests asserting on /transactions.json's displayed date string."""
    d = date.today() - timedelta(days=n)
    return f"{d:%b} {d.day}, {d.year}"

#: A small, hand-computable portfolio used across the endpoint tests, dated
#: relative to today (see _days_ago) so it stays within outlook.py's
#: trailing-12-month dividend window no matter when the suite actually runs:
#:
#: AAPL: buy 10 @ $100 (cost basis $1,000) -- still fully held, unrealized gain
#:       at the mocked $150 live price.
#: MSFT: buy 5 @ $200 (cost basis $1,000), sell 2 for $260 total proceeds
#:       (realized LOSS: $260 proceeds - $400 FIFO cost-of-shares-sold =
#:       -$140 -- see test_accounting.py for the exact FIFO math); 3 shares
#:       remain, cost basis $600 (3 x $200/share).
#: SCHD: one CDIV dividend of $50 (no share position -- pure income row),
#:       recent enough to count toward the TTM dividend yield.
#: Two ACH rows: a $5,000 deposit and a $1,000 withdrawal.
SEED_ROWS = [
    dict(activity_date=_days_ago(89), process_date=_days_ago(89), settle_date=_days_ago(87),
         symbol="AAPL", description="Buy AAPL", trans_code="Buy",
         quantity="10", price="$100.00", amount="-$1,000.00"),
    dict(activity_date=_days_ago(88), process_date=_days_ago(88), settle_date=_days_ago(86),
         symbol="MSFT", description="Buy MSFT", trans_code="Buy",
         quantity="5", price="$200.00", amount="-$1,000.00"),
    dict(activity_date=_days_ago(60), process_date=_days_ago(60), settle_date=_days_ago(58),
         symbol="MSFT", description="Sell MSFT", trans_code="Sell",
         quantity="2", price="$130.00", amount="$260.00"),
    dict(activity_date=_days_ago(30), process_date=_days_ago(30), settle_date=_days_ago(30),
         symbol="SCHD", description="Schwab US Dividend Equity ETF", trans_code="CDIV",
         quantity="", price="", amount="$50.00"),
    dict(activity_date=_days_ago(90), process_date=_days_ago(90), settle_date=_days_ago(90),
         symbol="", description="ACH Deposit", trans_code="ACH",
         quantity="", price="", amount="$5,000.00"),
    dict(activity_date=_days_ago(76), process_date=_days_ago(76), settle_date=_days_ago(76),
         symbol="", description="ACH Withdrawal", trans_code="ACH",
         quantity="", price="", amount="-$1,000.00"),
]

#: DB-first symbol classifications (stock_info) so holdings/sectors tests never
#: need a live yfinance call for name/sector/assetType.
SEED_STOCK_INFO = [
    dict(symbol="AAPL", name="Apple Inc.", sector="Technology", asset_type="Stock"),
    dict(symbol="MSFT", name="Microsoft Corporation", sector="Technology", asset_type="Stock"),
    dict(symbol="SCHD", name="Schwab U.S. Dividend Equity ETF", sector="ETF", asset_type="ETF"),
]

#: Mocked live prices matching the "unrealized gain on AAPL" fixture story.
SEED_PRICES = {"AAPL": 150.0, "MSFT": 210.0, "SCHD": 30.0}


@pytest.fixture(autouse=True)
def _reset_caches():
    """Clear both module-level TTLCache instances before every test so no test
    can observe (or poison) another test's cached endpoint responses or yfinance
    price-fetch results -- the in-memory caches are process-global, and without
    this reset a fast test that happens to run first could leave an entry with
    up to 5 minutes of TTL remaining for every later test in the same suite."""
    _app_cache._endpoint_cache.clear()
    market_data._price_cache.clear()


@pytest.fixture()
def seeded_env(tmp_path, monkeypatch):
    """Temp DB seeded with SEED_ROWS + SEED_STOCK_INFO, SessionLocal patched
    into every module that imported it directly (app.db, app.main -- mirrors
    the existing test_summary.py pattern's own comment on why both need it)."""
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Transaction.__table__.create(engine)
    StockInfo.__table__.create(engine)
    session_factory = sessionmaker(bind=engine)

    session = session_factory()
    try:
        session.add_all([Transaction(**row) for row in SEED_ROWS])
        session.add_all([StockInfo(**row) for row in SEED_STOCK_INFO])
        session.commit()
    finally:
        session.close()

    monkeypatch.setattr(db, "SessionLocal", session_factory)
    monkeypatch.setattr(main, "SessionLocal", session_factory)

    return {"session_factory": session_factory, "engine": engine}


@pytest.fixture()
def live_market_mock(monkeypatch):
    """Deterministic market data for every route that resolves live prices/
    classifications through app.market_data -- no live Yahoo Finance call,
    no dependency on stock_info being populated for a given test's symbols."""

    def fake_last_transaction_prices(symbols):
        return {sym: SEED_PRICES.get(sym, 0.0) for sym in symbols}

    def fake_fetch_live_prices(symbols, fallback_prices=None):
        return ({sym: SEED_PRICES.get(sym, 0.0) for sym in symbols}, "2026-06-01T12:00:00+00:00")

    def fake_get_symbol_classifications(symbols):
        by_symbol = {row["symbol"]: row for row in SEED_STOCK_INFO}
        return {
            sym: {
                "name": by_symbol.get(sym, {}).get("name"),
                "sector": by_symbol.get(sym, {}).get("sector"),
                "assetType": by_symbol.get(sym, {}).get("asset_type"),
            }
            for sym in symbols
        }

    monkeypatch.setattr(market_data, "last_transaction_prices", fake_last_transaction_prices)
    monkeypatch.setattr(market_data, "fetch_live_prices", fake_fetch_live_prices)
    monkeypatch.setattr(market_data, "get_symbol_classifications", fake_get_symbol_classifications)


@pytest.fixture()
def client(seeded_env, live_market_mock):
    with TestClient(main.app) as test_client:
        yield test_client
