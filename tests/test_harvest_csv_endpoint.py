"""End-to-end tests for GET /harvest_csv against a temporary SQLite DB."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import db, harvest as harvest_mod, main
from app.models import Transaction

HEADER = [
    "Activity Date", "Process Date", "Settle Date", "Instrument",
    "Description", "Trans Code", "Quantity", "Price", "Amount",
]


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    lines = [",".join(HEADER)]
    for row in rows:
        # Quote every field so embedded commas can't break the fixture shape.
        lines.append(",".join(f'"{field}"' for field in row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture()
def temp_harvest_env(tmp_path, monkeypatch):
    """Point the app's session factory and CSV glob at a throwaway SQLite DB.

    Builds the `transactions` table from the ORM model in a brand-new temp
    file database, writes two small fixture CSVs under tmp_path/csv/, and
    monkeypatches both SessionLocal references (app.db AND app.main — main.py
    imports the name directly) plus config.CSV_GLOB so harvest()'s default
    pattern resolves to the fixtures.
    """
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    _write_csv(csv_dir / "file_a.csv", [
        ["2024-01-01", "2024-01-01", "2024-01-03", "AAPL", "Buy AAPL 1 @ 100.00", "BUY", "1", "100.00", "-100.00"],
        ["2024-01-02", "2024-01-02", "2024-01-03", "MSFT", "Sell MSFT 2 @ 50.00", "SELL", "2", "50.00", "100.00"],
    ])
    _write_csv(csv_dir / "file_b.csv", [
        ["2024-02-01", "2024-02-01", "2024-02-03", "GOOG", "Buy GOOG 5 @ 10.00", "BUY", "5", "10.00", "-50.00"],
    ])

    db_file = tmp_path / "test.db"
    url = f"sqlite:///{db_file}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Transaction.__table__.create(engine)
    test_session_factory = sessionmaker(bind=engine)

    monkeypatch.setattr(db, "SessionLocal", test_session_factory)
    monkeypatch.setattr(main, "SessionLocal", test_session_factory)
    # harvest.py does `from app.config import CSV_GLOB`, so its module-global
    # name is what harvest()'s default pattern actually reads.
    monkeypatch.setattr(harvest_mod, "CSV_GLOB", str(csv_dir / "*.csv"))

    yield {
        "db_file": db_file,
        "engine": engine,
        "session_factory": test_session_factory,
        "expected_files": ["file_a.csv", "file_b.csv"],
        "expected_rows": {"file_a.csv": 2, "file_b.csv": 1},
    }


@pytest.fixture()
def client(temp_harvest_env):
    with TestClient(main.app) as test_client:
        yield test_client


def test_endpoint_returns_200_and_summary_shape(client, temp_harvest_env):
    response = client.get("/harvest_csv")

    assert response.status_code == 200
    body = response.json()
    # HarvestSummary shape: files list of {file, rows_loaded}, total_rows_loaded.
    assert set(body.keys()) >= {"files", "total_rows_loaded"}
    by_name = {entry["file"]: entry["rows_loaded"] for entry in body["files"]}
    expected_files = temp_harvest_env["expected_files"]
    assert sorted(by_name) == sorted(expected_files)
    assert by_name["file_a.csv"] == 2
    assert by_name["file_b.csv"] == 1
    assert body["total_rows_loaded"] == 3


def test_second_get_returns_identical_summary(client, temp_harvest_env):
    """Idempotency: truncating and re-harvesting on the second call must yield
    exactly the same per-file and total counts as the first."""
    first = client.get("/harvest_csv").json()
    second = client.get("/harvest_csv").json()

    assert first["total_rows_loaded"] == second["total_rows_loaded"]
    first_by_name = {e["file"]: e["rows_loaded"] for e in first["files"]}
    second_by_name = {e["file"]: e["rows_loaded"] for e in second["files"]}
    assert first_by_name == second_by_name


def test_transactions_table_matches_csv_exactly(client, temp_harvest_env):
    """After harvesting, the temp DB's `transactions` table must contain exactly
    the CSV rows — no stale leftovers from a prior harvest and no duplicates."""
    client.get("/harvest_csv")

    session = temp_harvest_env["session_factory"]()
    try:
        total = session.execute(text("SELECT COUNT(*) FROM transactions")).scalar_one()
        assert total == 3  # exactly the sum of fixture rows, nothing else

        distinct = session.execute(
            text("""
                SELECT activity_date, symbol, COUNT(*) AS n
                FROM transactions
                GROUP BY activity_date, symbol
                HAVING COUNT(*) > 1
            """)
        ).fetchall()
        assert distinct == []

        instruments = {
            row[0] for row in session.execute(
                text("SELECT DISTINCT symbol FROM transactions ORDER BY symbol")
            )
        }
        assert instruments == {"AAPL", "GOOG", "MSFT"}
    finally:
        session.close()
