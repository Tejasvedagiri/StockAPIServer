"""TestClient tests for the holdings-family endpoints: /transactions.json,
/holdings.json, /sectors.json, /sectorHoldings.json, and /growth.json (grouped
here rather than its own module since it shares the same build_holdings_
breakdown pipeline and fixture data as the other four).

Uses the shared `client`/`seeded_env`/`live_market_mock` fixtures from
conftest.py -- see SEED_ROWS there for the exact portfolio (AAPL fully held
w/ unrealized gain, MSFT partially sold w/ realized P&L, SCHD dividend-only,
plus ACH deposit/withdrawal).
"""

from tests.conftest import SEED_PRICES, _display_date


# ---------------------------------------------------------------------------
# 3.6.2 /transactions.json
# ---------------------------------------------------------------------------

class TestTransactionsJson:
    def test_every_field_is_a_string(self, client):
        rows = client.get("/transactions.json").json()
        assert rows, "fixture must produce at least one row"
        for row in rows:
            assert set(row.keys()) == {"type", "sym", "date", "shares", "price", "amount"}
            for value in row.values():
                assert isinstance(value, str)

    def test_dollar_amounts_use_x_xxx_xx_signed_format(self, client):
        rows = client.get("/transactions.json").json()
        buy = next(r for r in rows if r["type"] == "buy" and r["sym"] == "AAPL")
        assert buy["amount"] == "-$1,000.00"
        assert buy["price"] == "$100.00"
        assert buy["shares"] == "10"

    def test_dividend_row_uses_em_dash_for_shares_and_price(self, client):
        rows = client.get("/transactions.json").json()
        dividend = next(r for r in rows if r["type"] == "dividend")
        assert dividend["shares"] == "—"
        assert dividend["price"] == "—"
        assert dividend["amount"] == "+$50.00"

    def test_transfer_row_gets_a_non_empty_sym_label(self, client):
        """Regression test for the real bug found live: ACH/transfer rows
        have a blank Instrument column in the source, so `sym` used to come
        out "" -- it must now fall back to the row's own description."""
        rows = client.get("/transactions.json").json()
        transfers = [r for r in rows if r["type"] == "transfer"]
        assert transfers
        assert all(r["sym"] for r in transfers)

    def test_sorted_newest_first(self, client):
        rows = client.get("/transactions.json").json()
        dates = [r["date"] for r in rows]
        assert dates[0] == _display_date(30)  # the SCHD dividend, most recent in SEED_ROWS
        assert dates[-1] == _display_date(90)  # the ACH deposit, oldest


# ---------------------------------------------------------------------------
# 3.6.3 /holdings.json
# ---------------------------------------------------------------------------

class TestHoldingsJson:
    def test_field_set_and_types_match_api_md(self, client):
        rows = client.get("/holdings.json").json()
        assert rows
        for row in rows:
            assert set(row.keys()) == {
                "sym", "name", "shares", "avgCost", "price", "marketValue",
                "gainPct", "sector", "assetType",
            }
            for field in ("shares", "avgCost", "price", "marketValue", "gainPct"):
                assert isinstance(row[field], (int, float))
            for field in ("sym", "name", "sector", "assetType"):
                assert isinstance(row[field], str)

    def test_sold_out_symbol_absent_none_here_but_holds_present(self, client):
        rows = client.get("/holdings.json").json()
        syms = {r["sym"] for r in rows}
        assert "AAPL" in syms
        assert "MSFT" in syms  # partially sold, 3 shares remain

    def test_sector_and_name_come_from_stock_info(self, client):
        rows = client.get("/holdings.json").json()
        aapl = next(r for r in rows if r["sym"] == "AAPL")
        assert aapl["name"] == "Apple Inc."
        assert aapl["sector"] == "Technology"
        assert aapl["assetType"] == "Stock"

    def test_sorted_by_market_value_descending(self, client):
        rows = client.get("/holdings.json").json()
        values = [r["marketValue"] for r in rows]
        assert values == sorted(values, reverse=True)

    def test_msft_realized_partial_sale_reflected_in_remaining_shares(self, client):
        rows = client.get("/holdings.json").json()
        msft = next(r for r in rows if r["sym"] == "MSFT")
        assert msft["shares"] == 3.0  # 5 bought, 2 sold
        assert msft["price"] == SEED_PRICES["MSFT"]


# ---------------------------------------------------------------------------
# 3.6.4 /sectors.json
# ---------------------------------------------------------------------------

class TestSectorsJson:
    def test_shape_and_numeric_pct(self, client):
        rows = client.get("/sectors.json").json()
        assert rows
        for row in rows:
            assert set(row.keys()) == {"label", "pct", "color"}
            assert isinstance(row["pct"], (int, float))
            assert isinstance(row["label"], str)
            assert isinstance(row["color"], str)

    def test_percentages_sum_to_roughly_100(self, client):
        rows = client.get("/sectors.json").json()
        assert sum(r["pct"] for r in rows) == 100.0

    def test_labels_match_sector_holdings_keys(self, client):
        sector_labels = {r["label"] for r in client.get("/sectors.json").json()}
        holding_keys = set(client.get("/sectorHoldings.json").json().keys())
        assert sector_labels == holding_keys


# ---------------------------------------------------------------------------
# 3.6.5 /sectorHoldings.json
# ---------------------------------------------------------------------------

class TestSectorHoldingsJson:
    def test_is_an_object_keyed_by_sector_not_a_flat_array(self, client):
        body = client.get("/sectorHoldings.json").json()
        assert isinstance(body, dict)
        assert "Technology" in body  # AAPL + MSFT

    def test_each_row_shape_and_types(self, client):
        body = client.get("/sectorHoldings.json").json()
        for entries in body.values():
            for entry in entries:
                assert set(entry.keys()) == {"sym", "name", "value", "change", "pos"}
                assert isinstance(entry["value"], str) and entry["value"].startswith("$")
                assert isinstance(entry["change"], str) and entry["change"].endswith("%")
                assert entry["change"][0] in "+-"
                assert isinstance(entry["pos"], bool)

    def test_pos_flag_is_a_real_boolean_not_a_string(self, client):
        body = client.get("/sectorHoldings.json").json()
        aapl = next(e for e in body["Technology"] if e["sym"] == "AAPL")
        assert aapl["pos"] is True  # AAPL is priced above avg cost in the fixture


# ---------------------------------------------------------------------------
# 3.6.8 /growth.json
# ---------------------------------------------------------------------------

class TestGrowthJson:
    def test_shape_is_sym_and_numeric_pct(self, client):
        rows = client.get("/growth.json").json()
        assert rows
        for row in rows:
            assert set(row.keys()) == {"sym", "pct"}
            assert isinstance(row["sym"], str)
            assert isinstance(row["pct"], (int, float))

    def test_matches_holdings_gain_pct_exactly(self, client):
        growth = {r["sym"]: r["pct"] for r in client.get("/growth.json").json()}
        holdings = {r["sym"]: r["gainPct"] for r in client.get("/holdings.json").json()}
        assert growth == holdings
