"""ORM models mapped onto the existing database schema."""

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Transaction(Base):
    """Maps onto the pre-existing `transactions` table (all TEXT data cols).

    The physical production table has NO user-declared primary key and every
    data column is a plain nullable TEXT — Robinhood exports contain many rows
    sharing one activity date plus 37 fully-duplicate row copies, so no PK/UNIQUE
    may be declared on any of the nine data columns: ORM bulk insert of a fresh
    harvest must commit every CSV row unchanged.

    SQLAlchemy's declarative still requires *some* primary key for mapper
    identity and autoflush bookkeeping, so ``rowid`` maps to SQLite's built-in
    integer rowid (an alias column — it is not an extra data column; the DDL on
    disk stays exactly nine plain TEXT columns). The existing production table
    is never migrated — only read from and written to.
    """

    __tablename__ = "transactions"

    # SQLite rowid alias: PK for ORM identity only, not a physical data column.
    rowid: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Mapped[str | None] + Text: production's columns are plain nullable TEXT;
    # sqlalchemy's String would emit VARCHAR instead of TEXT on create_all.
    activity_date: Mapped[str | None] = mapped_column(Text)
    process_date: Mapped[str | None] = mapped_column(Text)
    settle_date: Mapped[str | None] = mapped_column(Text)
    symbol: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    trans_code: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[str | None] = mapped_column(Text)
    price: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[str | None] = mapped_column(Text)


class StockInfo(Base):
    """Cached per-symbol classification, mirroring StockMcpServer's stock_info.

    DB-first read-through table (see app/market_data.py): a row here means no
    live yfinance info call is ever made for that symbol again. ``raw_info``
    stores the full yfinance Ticker.info dict JSON-verbatim so no fetched field
    is lost; the dedicated columns hold the final classification already
    resolved (SECTOR_MAP fallback included).
    """

    __tablename__ = "stock_info"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str | None] = mapped_column(String)
    sector: Mapped[str | None] = mapped_column(String)
    asset_type: Mapped[str | None] = mapped_column(String)
    quote_type: Mapped[str | None] = mapped_column(String)
    currency: Mapped[str | None] = mapped_column(String)
    exchange: Mapped[str | None] = mapped_column(String)
    raw_info: Mapped[str | None] = mapped_column(String)  # JSON-encoded Ticker.info dict
    updated_at: Mapped[str | None] = mapped_column(String)  # ISO-8601 UTC write time
