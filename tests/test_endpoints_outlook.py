"""TestClient tests for GET /outlookOn.json + GET /outlookOff.json (3.6.7).

Both endpoints compute live from the real FIFO cost basis + TTM dividend
yield (see app/routes/outlook.py) -- no static seed -- so these tests assert
the documented positional flat-string-array shape and, critically, that the
two variants genuinely differ (the real bug found and fixed during plan
section 2's live verification: they used to be byte-identical).
"""


class TestOutlookEndpoints:
    def test_both_endpoints_return_positional_flat_string_arrays(self, client):
        for path in ("/outlookOn.json", "/outlookOff.json"):
            rows = client.get(path).json()
            assert len(rows) == 6
            for row in rows:
                assert isinstance(row, list)
                assert len(row) == 4
                assert all(isinstance(cell, str) for cell in row)
                # No object field names anywhere in the payload.
                assert not isinstance(row, dict)

    def test_no_named_fields_anywhere_in_the_raw_json(self, client):
        """API.md's documented quirk: positional, not named objects."""
        for path in ("/outlookOn.json", "/outlookOff.json"):
            text = client.get(path).text
            assert "horizon" not in text
            assert "dividendsTotal" not in text
            assert "reinvestedTotal" not in text
            assert "projectedTotal" not in text

    def test_on_and_off_are_not_a_copy_of_each_other(self, client):
        on_rows = client.get("/outlookOn.json").json()
        off_rows = client.get("/outlookOff.json").json()
        assert on_rows != off_rows

    def test_on_projects_a_larger_or_equal_total_than_off_at_every_horizon(self, client):
        """The fixture (SEED_ROWS in conftest) has a real dividend (SCHD
        CDIV $50) inside the trailing 12 months, so reinvesting it must
        never project a SMALLER total than not reinvesting -- equal only if
        the TTM yield happens to compute to exactly zero."""
        on_rows = client.get("/outlookOn.json").json()
        off_rows = client.get("/outlookOff.json").json()

        def total(row):
            return float(row[3].replace("$", "").replace(",", ""))

        for on_row, off_row in zip(on_rows, off_rows):
            assert total(on_row) >= total(off_row)

    def test_horizon_labels_match_across_both_variants(self, client):
        on_rows = client.get("/outlookOn.json").json()
        off_rows = client.get("/outlookOff.json").json()
        assert [r[0] for r in on_rows] == [r[0] for r in off_rows]
        assert [r[0] for r in on_rows] == [
            "5 years", "10 years", "15 years", "20 years", "25 years", "30 years",
        ]
