# Route handlers for the API.md REST contract (9 endpoints).
# Routers are defined in sibling modules and re-exported here.
from app.routes.holdings import router as holdings_router  # noqa: F401
from app.routes.news import router as news_router  # noqa: F401
from app.routes.outlook import router as outlook_router  # noqa: F401
from app.routes.summary import router as summary_router  # noqa: F401
from app.routes.transactions import router as transactions_router  # noqa: F401

__all__ = [
    "holdings_router",
    "news_router",
    "outlook_router",
    "summary_router",
    "transactions_router",
]
