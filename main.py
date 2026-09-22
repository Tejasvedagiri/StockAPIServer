"""StockAPIServer entrypoint.

The FastAPI app lives in ``app.main`` (single source of truth for all routes:
GET /stocks, GET /harvest_csv, and the API.md contract endpoints). This module
only starts uvicorn against that same app object so there is never a second,
divergent app instance to maintain.

Run with:  python main.py   (or:  uvicorn app.main:app)
"""

from constants.server import SERVER_HOST, SERVER_PORT


def main() -> None:
    import uvicorn

    from app.main import app

    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)


if __name__ == "__main__":
    main()
