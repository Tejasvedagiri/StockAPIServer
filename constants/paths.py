"""Path-related constants (pure data, no behavior)."""

# Default database connection URL; overridable via the STOCKMCP_DATABASE_URL
# environment variable (see app.config for where that override is applied).
DEFAULT_DATABASE_URL = "sqlite:///data/stockmcp.db"

# Name of the environment variable that overrides DEFAULT_DATABASE_URL.
DATABASE_URL_ENV_VAR = "STOCKMCP_DATABASE_URL"

# Glob pattern matching the Robinhood CSV exports to harvest.
CSV_GLOB = "resources/robinhood/*.csv"
