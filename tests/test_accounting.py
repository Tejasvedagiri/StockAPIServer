"""Unit tests for app/accounting.py's pure engine -- no DB, no HTTP, no I/O.

Covers plan section 3.5: map_trans_code (one case per real code present in
resources/robinhood/*.csv), the FIFO engine's multi-lot sell + string-quantity
edge case, compute_portfolio_metrics' rounding rules, and
compute_outlook_projections' positional-string-array shape (including that the
two variants genuinely differ, per the real bug found and fixed in
app/accounting.py during plan section 2's live verification).
"""

from app.accounting import (
    TRANS_CODE_TO_TYPE,
    build_holdings_breakdown,
    compute_outlook_projections,
    compute_portfolio_metrics,
    map_trans_code,
)


# ---------------------------------------------------------------------------
# 3.5.1 map_trans_code -- one case per code actually present in the real CSVs
# ---------------------------------------------------------------------------

class TestMapTransCode:
    def test_buy(self):
        assert map_trans_code("Buy") == "buy"

    def test_sell(self):
        assert map_trans_code("Sell") == "sell"

    def test_spl_stock_split_maps_to_buy(self):
        """Documented approximation: a split adds shares with no cash flow,
        closest of the 4 categories is 'buy' -- not a bug, see TRANS_CODE_TO_TYPE."""
        assert map_trans_code("SPL") == "buy"

    def test_cash_dividend(self):
        assert map_trans_code("CDIV") == "dividend"

    def test_dividend_reinvestment_rec(self):
        assert map_trans_code("REC") == "dividend"

    def test_nra_tax_withholding_is_dividend(self):
        assert map_trans_code("NRAT") == "dividend"

    def test_brokerage_interest_is_dividend(self):
        assert map_trans_code("INT") == "dividend"

    def test_stock_lending_income_is_dividend(self):
        assert map_trans_code("SLIP") == "dividend"

    def test_gold_deposit_boost_payment_is_dividend(self):
        assert map_trans_code("GDBP") == "dividend"

    def test_ach_bank_transfer(self):
        assert map_trans_code("ACH") == "transfer"

    def test_robinhood_credit_card(self):
        assert map_trans_code("XENT_CC") == "transfer"

    def test_robinhood_card_noa(self):
        assert map_trans_code("NOA") == "transfer"

    def test_instant_bank_transfer_rtp(self):
        assert map_trans_code("RTP") == "transfer"

    def test_brokerage_to_brokerage_itrf(self):
        assert map_trans_code("ITRF") == "transfer"

    def test_unknown_code_falls_back_to_transfer_and_never_crashes(self):
        assert map_trans_code("SOME_FUTURE_CODE") == "transfer"

    def test_every_mapped_value_is_one_of_the_four_activity_types(self):
        assert set(TRANS_CODE_TO_TYPE.values()) <= {"buy", "sell", "dividend", "transfer"}


# ---------------------------------------------------------------------------
# 3.5.2 build_holdings_breakdown -- FIFO lot consumption
# ---------------------------------------------------------------------------

class TestFifoLotConsumption:
    def test_sell_exhausting_multiple_lots_charges_each_at_its_own_cost(self):
        """Two buys at different prices, then a sell that eats all of the
        first lot and part of the second -- realized P&L must charge the
        correct blended cost-per-share, not average across all lots."""
        rows = [
            {"activity_date": "01/01/2024", "sym": "XYZ", "trans_code": "Buy", "quantity": "10", "price": "$10.00", "amount": "-$100.00"},
            {"activity_date": "01/02/2024", "sym": "XYZ", "trans_code": "Buy", "quantity": "10", "price": "$20.00", "amount": "-$200.00"},
            # Sells 15: all 10 of lot 1 (cost $10/sh) + 5 of lot 2 (cost $20/sh)
            # = $100 + $100 = $200 cost basis consumed. Sale proceeds $300 (an
            # even $20/share) -> realized gain = $300 - $200 = $100.
            {"activity_date": "01/03/2024", "sym": "XYZ", "trans_code": "Sell", "quantity": "15", "price": "$20.00", "amount": "$300.00"},
        ]
        metrics = compute_portfolio_metrics(rows)
        assert metrics["realizedGainLoss"] == 100.0
        assert metrics["sellCount"] == 1
        assert metrics["winningSellCount"] == 1
        # 5 shares remain from lot 2, at $20/share cost basis = $100.
        assert metrics["remaining"]["XYZ"] == 5.0
        assert metrics["perSymbol"]["XYZ"]["costBasis"] == 100.0

    def test_quantity_as_text_needing_float_conversion(self):
        """Quantities arrive as raw CSV strings (sometimes fractional, e.g.
        DRIP/split remainders) -- parse_accounting_number must convert them,
        never leave a comparison silently doing string math."""
        rows = [
            {"activity_date": "01/01/2024", "sym": "ABC", "trans_code": "Buy", "quantity": "0.140999", "price": "$709.22", "amount": "-$100.00"},
        ]
        metrics = compute_portfolio_metrics(rows)
        assert metrics["remaining"]["ABC"] == 0.140999
        assert metrics["holdingsCount"] == 1

    def test_build_holdings_breakdown_computes_avg_cost_and_gain_pct(self):
        rows = [
            {"activity_date": "01/01/2024", "sym": "AAPL", "trans_code": "Buy", "quantity": "10", "price": "$100.00", "amount": "-$1,000.00"},
        ]
        breakdown = build_holdings_breakdown(rows, prices={"AAPL": 150.0}, classifications={})
        [row] = breakdown
        assert row["sym"] == "AAPL"
        assert row["shares"] == 10.0
        assert row["avgCost"] == 100.0
        assert row["price"] == 150.0
        assert row["marketValue"] == 1500.0
        assert row["gainPct"] == 50.0  # (150-100)/100 * 100

    def test_sold_out_symbol_never_appears_in_breakdown(self):
        rows = [
            {"activity_date": "01/01/2024", "sym": "XYZ", "trans_code": "Buy", "quantity": "10", "price": "$10.00", "amount": "-$100.00"},
            {"activity_date": "01/02/2024", "sym": "XYZ", "trans_code": "Sell", "quantity": "10", "price": "$12.00", "amount": "$120.00"},
        ]
        breakdown = build_holdings_breakdown(rows, prices={"XYZ": 12.0}, classifications={})
        assert breakdown == []

    def test_missing_live_price_values_holding_at_zero_not_crash(self):
        rows = [
            {"activity_date": "01/01/2024", "sym": "NOPRICE", "trans_code": "Buy", "quantity": "5", "price": "$10.00", "amount": "-$50.00"},
        ]
        breakdown = build_holdings_breakdown(rows, prices={}, classifications={})
        [row] = breakdown
        assert row["price"] == 0.0
        assert row["marketValue"] == 0.0

    def test_classification_fallbacks_when_stock_info_has_no_row(self):
        rows = [
            {"activity_date": "01/01/2024", "sym": "MYSTERY", "trans_code": "Buy", "quantity": "1", "price": "$10.00", "amount": "-$10.00"},
        ]
        breakdown = build_holdings_breakdown(rows, prices={"MYSTERY": 10.0}, classifications={})
        [row] = breakdown
        assert row["name"] == "MYSTERY"  # falls back to the symbol itself
        assert row["sector"] == "Other"
        assert row["assetType"] == "Stock"


# ---------------------------------------------------------------------------
# 3.5.3 compute_portfolio_metrics -- rounding / return-type rules
# ---------------------------------------------------------------------------

class TestPortfolioMetricsRounding:
    def test_all_top_level_money_fields_are_real_numbers_not_strings(self):
        rows = [
            {"activity_date": "01/01/2024", "sym": "AAPL", "trans_code": "Buy", "quantity": "10", "price": "$100.00", "amount": "-$1,000.00"},
            {"activity_date": "01/02/2024", "sym": "", "trans_code": "ACH", "quantity": "", "price": "", "amount": "$500.00"},
            {"activity_date": "01/03/2024", "sym": "SCHD", "trans_code": "CDIV", "quantity": "", "price": "", "amount": "$12.34"},
        ]
        metrics = compute_portfolio_metrics(rows)
        numeric_fields = (
            "realizedGainLoss", "totalWin", "totalLoss", "costBasis",
            "dividendsTotal", "totalDeposited", "cashAvailable",
        )
        for field in numeric_fields:
            assert isinstance(metrics[field], float), f"{field} must be a float"

    def test_dividends_total_nets_nra_withholding(self):
        """NRAT (tax withholding) is mapped to 'dividend' too and carries a
        NEGATIVE amount -- it must net against CDIV, not be dropped/ignored."""
        rows = [
            {"activity_date": "01/01/2024", "sym": "SCHD", "trans_code": "CDIV", "quantity": "", "price": "", "amount": "$100.00"},
            {"activity_date": "01/01/2024", "sym": "SCHD", "trans_code": "NRAT", "quantity": "", "price": "", "amount": "-$15.00"},
        ]
        metrics = compute_portfolio_metrics(rows)
        assert metrics["dividendsTotal"] == 85.0

    def test_ach_withdrawal_never_counted_in_total_deposited(self):
        rows = [
            {"activity_date": "01/01/2024", "sym": "", "trans_code": "ACH", "quantity": "", "price": "", "amount": "$1,000.00"},
            {"activity_date": "01/02/2024", "sym": "", "trans_code": "ACH", "quantity": "", "price": "", "amount": "-$400.00"},
        ]
        metrics = compute_portfolio_metrics(rows)
        assert metrics["totalDeposited"] == 1000.0
        assert metrics["cashAvailable"] == 600.0  # deposit minus withdrawal, netted


# ---------------------------------------------------------------------------
# 3.5.4 compute_outlook_projections -- positional string-array output shape
# ---------------------------------------------------------------------------

class TestOutlookProjectionsShape:
    def test_returns_flat_positional_string_arrays_no_named_fields(self):
        off_rows, on_rows = compute_outlook_projections(cost_basis=50_000.0, ttm_dividend_yield_pct=0.5)
        for rows in (off_rows, on_rows):
            assert len(rows) == 6  # PROJECTION_HORIZONS: 5/10/15/20/25/30
            for row in rows:
                assert isinstance(row, list)
                assert len(row) == 4
                assert all(isinstance(cell, str) for cell in row)

    def test_horizon_labels_match_documented_years(self):
        off_rows, _ = compute_outlook_projections(cost_basis=10_000.0, ttm_dividend_yield_pct=0.0)
        assert [row[0] for row in off_rows] == [
            "5 years", "10 years", "15 years", "20 years", "25 years", "30 years",
        ]

    def test_on_and_off_variants_genuinely_differ_when_dividend_yield_is_nonzero(self):
        """Regression test for the real bug found live: a nonzero dividend
        yield used to produce byte-identical off/on rows. They must now
        differ, with "on" (reinvested) projecting a larger total at every
        horizon than "off" (not reinvested)."""
        off_rows, on_rows = compute_outlook_projections(cost_basis=50_000.0, ttm_dividend_yield_pct=2.0)
        assert off_rows != on_rows
        for off_row, on_row in zip(off_rows, on_rows):
            off_total = float(off_row[3].replace("$", "").replace(",", ""))
            on_total = float(on_row[3].replace("$", "").replace(",", ""))
            assert on_total > off_total

    def test_zero_dividend_yield_still_produces_growth(self):
        """No dividends at all: 'on' and 'off' converge on column 2 (both
        just base-rate growth), but column 1 (cumulative dividends) is
        correctly zero, not a crash or a negative number."""
        off_rows, on_rows = compute_outlook_projections(cost_basis=10_000.0, ttm_dividend_yield_pct=0.0)
        assert off_rows[0][1] == "$0.00"
        assert on_rows[0][1] == "$0.00"
        assert off_rows[0][2] == on_rows[0][2]

    def test_negative_yield_is_clamped_to_zero_not_negative_growth(self):
        off_rows, on_rows = compute_outlook_projections(cost_basis=10_000.0, ttm_dividend_yield_pct=-5.0)
        assert off_rows[0][2] == on_rows[0][2]  # no reinvestment boost applied
