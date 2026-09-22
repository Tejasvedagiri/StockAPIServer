"""Harvest Robinhood CSV exports into the `transactions` table via ORM."""

import csv
import glob
import logging
import os

from sqlalchemy import delete

from app.config import CSV_GLOB
from app.models import Transaction
from constants.columns import COLUMN_MAP

logger = logging.getLogger(__name__)


def map_header(header):
    """Validate the CSV header against COLUMN_MAP; return {csv_col: target_col}.

    Fails fast (naming every unknown column) if any header column is not in
    the map, so a re-shape of the export is noticed immediately.
    """
    unknown = [col for col in header if col not in COLUMN_MAP]
    if unknown:
        raise ValueError(f"unknown CSV header column(s), not in mapping: {unknown}")
    return {col: COLUMN_MAP[col] for col in header}


def parse_csv(path):
    """Parse one Robinhood export; return (header_map, data_records).

    Row 0 is the header and never enters the data path. Records whose field
    count does not equal the header width are dropped — this single rule
    removes both the trailing blank row and the footer/disclaimer rows.
    Opening with newline="" keeps quoted embedded newlines (e.g. inside
    Description) within their field instead of splitting them into new rows.
    """
    col_map = None
    kept, dropped = [], []
    with open(path, newline="", encoding="utf-8") as fh:
        for record in csv.reader(fh):
            if col_map is None:
                col_map = map_header(record)  # row 0 = header, consumed here
                continue
            if len(record) == len(col_map):
                kept.append(record)
            else:
                dropped.append(record)

    logger.info("%s: %d rows kept, %d dropped", path, len(kept), len(dropped))
    for i, rec in enumerate(dropped, 1):
        preview = " | ".join(rec)[:80]
        logger.debug("dropped #%d (%d fields): %r", i, len(rec), preview)

    return col_map, kept


def discover_csv_files(pattern):
    """Return the sorted list of CSV files matching *pattern*.

    Raises FileNotFoundError (with a clear message) if nothing matches, so
    an empty harvest never silently "succeeds".
    """
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"no CSV files found matching {pattern!r}")
    logger.info("found %d CSV file(s) to harvest", len(files))
    return files


def load_file(session, path):
    """Bulk-insert one CSV file's rows into the (already truncated) table.

    Called from harvest() inside that function's single transaction: the
    truncate happens once before any file is loaded, so a failure leaves
    either the old rows or the new ones — never a half-state. Returns the
    number of rows inserted.
    """
    col_map, records = parse_csv(path)

    # Bulk insert via ORM objects. `col_map` preserves the original header
    # order, so list(col_map) gives us the CSV column names in file order —
    # rows are mapped by named column from that map, never positional
    # guesswork or raw INSERT strings.
    headers = list(col_map)
    session.add_all(
        Transaction(**{col_map[header]: record[i] for i, header in enumerate(headers)})
        for record in records
    )

    logger.info("loaded %d rows from %s", len(records), path)
    return len(records)


def harvest(session, pattern=None):
    """Truncate-and-reload the transactions table from every matching CSV.

    The table is truncated once up front, then each file's rows are inserted
    and committed in its own transaction, so one bad file cannot leave a
    previously-loaded file's data rolled back. Returns a summary dict of
    per-file row counts plus totals — see app/schemas.py for the Pydantic
    shape this maps onto.
    """
    pattern = pattern or CSV_GLOB
    files = discover_csv_files(pattern)

    # Truncate once, before any file is loaded (SQLite has no TRUNCATE; a
    # full DELETE is the equivalent). Done in this session so harvest()'s
    # per-file commits also commit the cleared table.
    session.execute(delete(Transaction))

    file_summaries = []
    total_rows = 0
    for path in files:
        rows_loaded = load_file(session, path)
        session.commit()  # commit per file — see docstring above
        total_rows += rows_loaded
        file_summaries.append({"file": os.path.basename(path), "rows_loaded": rows_loaded})

    summary = {
        "files": file_summaries,
        "total_rows_loaded": total_rows,
        "files_processed": len(files),
    }
    logger.info("harvest complete: %d files, %d total rows loaded",
                len(files), total_rows)
    return summary

