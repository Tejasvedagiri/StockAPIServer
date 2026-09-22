from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import data
from app.db import Base, SessionLocal, engine
from app.harvest import harvest
# Import the ORM models so both tables are registered on Base.metadata before
# create_all runs below — this is what guarantees stock_info (and transactions)
# exist in the DB file as soon as the app module is imported at startup.
from app.models import StockInfo, Transaction  # noqa: F401
from app.routes import holdings_router, news_router, outlook_router, summary_router, transactions_router
from constants.server import SERVER_HOST, SERVER_PORT

Base.metadata.create_all(engine)

app = FastAPI()

# API.md "CORS": every endpoint must allow the frontend's origin so plain
# browser fetch() works. The Vite dev server serves the UI on port 5173;
# the backend itself is pointed at via VITE_API_URL=http://127.0.0.:3334,
# so both localhost and loopback forms of that origin are allowed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        # Vite dev server runs on a fixed port 3333 (run.sh) — the actual UI origin.
        # :5173 entries kept as fallback for default-port invocations.
        "http://localhost:3333",
        "http://127.0.0.1:3333",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return f"Hello, World! from uv run app on port {SERVER_PORT}"


ALLOWED_TABLES = {"live", "info", "ohlc"}


def _resolve_symbols(symbols_param):
    """Normalize the symbols query param; fall back to DEFAULT_SYMBOLS."""
    if not symbols_param or not symbols_param.strip():
        return list(data.DEFAULT_SYMBOLS)
    cleaned = [s.strip() for s in symbols_param.split(",")]
    cleaned = [s for s in cleaned if s]
    return cleaned or list(data.DEFAULT_SYMBOLS)


@app.get("/stocks")
def stocks(
    table: str = Query(...),
    symbols: str | None = Query(None),
    timerange: str = Query("5d"),
    interval: str = Query("1d"),
):
    """Unified endpoint over the 3 tables (live / info / ohlc)."""
    if table not in ALLOWED_TABLES:
        return JSONResponse(
            status_code=422,
            content={
                "detail": f"invalid table {table!r}; must be one of the allowed values",
                "allowed_tables": sorted(ALLOWED_TABLES),
            },
        )
    symbol_list = _resolve_symbols(symbols)
    if table == "live":
        return data.get_live_prices(symbol_list)
    if table == "info":
        return data.get_stock_info(symbol_list)
    # table == "ohlc" (only remaining allowed value after the check above)
    return data.get_ohlc(symbol_list, timerange=timerange, interval=interval)


@app.get("/harvest_csv")
def harvest_csv():
    session = SessionLocal()
    try:
        return harvest(session)
    finally:
        session.close()


def main() -> None:
    """Run the server bound to constants.server settings; blocks until shutdown."""
    import uvicorn

    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)


# API.md contract routes: mounted at ROOT (frontend fetches ${VITE_API_URL}/holdings.json etc., no /api segment).
app.include_router(holdings_router)
app.include_router(news_router)
app.include_router(outlook_router)
app.include_router(summary_router)
app.include_router(transactions_router)
