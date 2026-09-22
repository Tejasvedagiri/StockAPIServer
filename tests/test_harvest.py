"""Unit tests for app.harvest (header mapping, CSV parsing)."""

import logging

import pytest

from app import harvest


VALID_HEADER = [
    "Activity Date",
    "Process Date",
    "Settle Date",
    "Instrument",
    "Description",
    "Trans Code",
    "Quantity",
    "Price",
    "Amount",
]


def test_map_header_full_valid_header_returns_column_mapping():
    result = harvest.map_header(VALID_HEADER)

    assert result == {
        "Activity Date": "activity_date",
        "Process Date": "process_date",
        "Settle Date": "settle_date",
        "Instrument": "symbol",
        "Description": "description",
        "Trans Code": "trans_code",
        "Quantity": "quantity",
        "Price": "price",
        "Amount": "amount",
    }


def test_map_header_unknown_column_raises_error_naming_it():
    header = VALID_HEADER[:3] + ["Weird Column"]

    with pytest.raises(ValueError) as excinfo:
        harvest.map_header(header)

    assert "Weird Column" in str(excinfo.value)


ROW_A = ["2024-01-05", "2024-01-05", "2024-01-08", "AAPL", "Buy AAPL", "BUY", "1", "23.50", "-23.50"]
ROW_B = ["2024-01-10", "2024-01-10", "2024-01-11", "MSFT", "Sell MSFT\nsecond line", "SELL", "2", "40.00", "80.00"]


def _write_fixture_csv(path, rows):
    lines = [",".join(VALID_HEADER)]
    for row in rows:
        # Quote any field containing a newline or comma; simple fields stay bare.
        def q(field):
            if "\n" in field or "," in field:
                return '"' + field.replace('"', '""') + '"'
            return field
        lines.append(",".join(q(f) for f in row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_csv_keeps_all_valid_rows_exactly(tmp_path):
    csv_file = tmp_path / "fixture.csv"
    _write_fixture_csv(csv_file, [ROW_A, ROW_B])

    col_map, records = harvest.parse_csv(str(csv_file))

    assert records == [ROW_A, ROW_B]  # kept rows match input exactly, in order


def test_parse_csv_multi_line_quoted_description_stays_one_record(tmp_path):
    csv_file = tmp_path / "multiline.csv"
    _write_fixture_csv(csv_file, [ROW_A, ROW_B])

    _, records = harvest.parse_csv(str(csv_file))

    # The embedded newline in ROW_B's Description must not split it: exactly 2 rows.
    assert len(records) == 2
    assert "Sell MSFT\nsecond line" in records[1]


def test_parse_csv_logs_keep_and_drop_counts_via_logger_not_print(tmp_path, caplog):
    csv_file = tmp_path / "with_junk.csv"
    short_row = ["2024-02-01", "2024-02-01", "AAPL"]           # 3 fields: junk
    footer_row = ["Footer", "text", "here", "extra"]           # 4 fields: junk
    _write_fixture_csv(csv_file, [ROW_A, ROW_B, short_row, footer_row])

    with caplog.at_level("INFO"):
        _, records = harvest.parse_csv(str(csv_file))

    assert len(records) == 2  # only the two valid rows were kept
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    kept_line = next((r for r in info_records if "rows kept" in r.getMessage()), None)
    assert kept_line is not None, (
        f"expected an INFO line about kept rows, got: {[r.getMessage() for r in caplog.records]}"
    )
    # 2 valid rows kept, 2 junk rows dropped — both counts present on the same line.
    assert str(kept_line.args[1]) == "2" and str(kept_line.args[2]) == "2"

    # No print() anywhere in the module source: counts are only ever logged.
    import inspect
    src = inspect.getsource(harvest)
    assert "print(" not in src.replace("keep/drop", ""), "harvest.py uses print()"


def test_parse_csv_excludes_short_trailing_and_over_long_footer_rows(tmp_path):
    csv_file = tmp_path / "edge.csv"
    short_row = ["2024-03-01"]                                  # too few fields
    footer_row = ["Footer", "a", "b", "c", "d"]                # more fields than header
    _write_fixture_csv(csv_file, [ROW_A, short_row, footer_row])

    _, records = harvest.parse_csv(str(csv_file))

    assert records == [ROW_A]  # both junk rows excluded from returned records


# --- Integration: harvest() against a temporary SQLite DB seeded via ORM ---

DUMMY_ROW = ["2000-01-01", "2000-01-01", "2000-01-02", "OLD", "stale row", "HOLD", "9", "1.00", "-9.00"]
ROW_C = ["2024-04-01", "2024-04-01", "2024-04-02", "TSLA", "Buy TSLA", "BUY", "3", "180.00", "-540.00"]
ROW_D = ["2024-04-15", "2024-04-15", "2024-04-16", "NVDA", "Sell NVDA", "SELL", "5", "87.25", "436.25"]
ROW_E = ["2024-04-20", "2024-04-20", "2024-04-21", "AMD", "Buy AMD", "BUY", "10", "95.00", "-950.00"]


def _make_session(tmp_path):
    """A fresh session bound to a temp SQLite DB with the transactions table."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db import Base
    # Imported for its side effect: registers Transaction on Base.metadata
    # before create_all runs. Also already imported by `app.harvest` at
    # this file's own module level, but kept explicit here so this
    # function's own dependency is never accidentally silent.
    from app.models import Transaction  # noqa: F401

    engine = create_engine(f"sqlite:///{tmp_path / 'test_harvest.db'}")
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    return session


def _activity_dates_in(session):
    from sqlalchemy import select

    from app.models import Transaction

    rows = session.execute(select(Transaction)).scalars().all()
    return sorted(r.activity_date for r in rows)


def test_harvest_truncates_and_reloads_without_duplicates_or_stale_rows(tmp_path):
    """Pre-insert dummy rows, harvest fixture CSVs: table holds exactly all
    the fresh rows from every file — old rows gone, no duplicates.
    """
    _write_fixture_csv(tmp_path / "first.csv", [ROW_A, ROW_B])
    _write_fixture_csv(tmp_path / "second.csv", [ROW_C, ROW_D, ROW_E])

    session = _make_session(tmp_path)
    # Seed a stale row that the harvest must wipe out.
    from app.models import Transaction
    session.add(Transaction(activity_date=DUMMY_ROW[0], process_date=DUMMY_ROW[1],
                            settle_date=DUMMY_ROW[2], symbol=DUMMY_ROW[3],
                            description=DUMMY_ROW[4], trans_code=DUMMY_ROW[5],
                            quantity=DUMMY_ROW[6], price=DUMMY_ROW[7], amount=DUMMY_ROW[8]))
    session.commit()

    try:
        harvest.harvest(session, pattern=str(tmp_path / "*.csv"))
    finally:
        session.close()

    fresh = _make_session(tmp_path)
    try:
        dates = _activity_dates_in(fresh)
        assert "2000-01-01" not in dates                      # stale row gone (truncated once up front)
        expected_all = sorted([ROW_A[0], ROW_B[0]] + [ROW_C[0], ROW_D[0], ROW_E[0]])  # every file's rows
        assert dates == expected_all                          # exactly the fresh rows, no dupes
    finally:
        fresh.close()


def test_harvest_summary_reports_per_file_counts_and_total(tmp_path):
    _write_fixture_csv(tmp_path / "first.csv", [ROW_A, ROW_B])       # 2 data rows
    _write_fixture_csv(tmp_path / "second.csv", [ROW_C, ROW_D, ROW_E])  # 3 data rows

    session = _make_session(tmp_path)
    try:
        summary = harvest.harvest(session, pattern=str(tmp_path / "*.csv"))
    finally:
        session.close()

    assert summary["files_processed"] == 2
    per_file = {entry["file"]: entry["rows_loaded"] for entry in summary["files"]}
    assert per_file == {"first.csv": 2, "second.csv": 3}  # honest per-file counts
    assert summary["total_rows_loaded"] == 5              # summed across both files
