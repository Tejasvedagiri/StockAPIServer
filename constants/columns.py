"""CSV column mapping for Robinhood exports (pure data, no behavior)."""

# Explicit CSV header name -> transactions column, for all 9 columns.
COLUMN_MAP = {
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
