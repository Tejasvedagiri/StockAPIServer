"""Central configuration for the app (DB URL, CSV sources)."""

import os

from constants.paths import (
    DATABASE_URL_ENV_VAR,
    DEFAULT_DATABASE_URL,
    CSV_GLOB,  # noqa: F401 -- re-exported: app/harvest.py does `from app.config import CSV_GLOB`
)

# Database connection URL. Overridable via environment variable; defaults to
# the existing SQLite database (see constants.paths.DEFAULT_DATABASE_URL).
DATABASE_URL = os.getenv(DATABASE_URL_ENV_VAR, DEFAULT_DATABASE_URL)
