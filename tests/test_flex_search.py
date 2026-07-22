import unittest
from calendar import monthrange
from unittest.mock import patch
import json

import app as flight_app


class FlexSearchTests(unittest.TestCase):
    def setUp(self):
        for cache_name in (
            "FLEX_RESULT_CACHE",
            "CHEAPEST_SNAPSHOT_CACHE",
            "RAW_SEARCH_CACHE",
            "AMADEUS_FAILURE_CACHE",
        ):
            cache = getattr(flight_app, cache_name, None)
            if cache is None:
                continue
            if hasattr(cache, "clear"):
                cache.clear()
            elif hasattr(cache, "_data"):
                cache._data.clear()

    def test_light_cheapest_for_date_uses_snapshot_cache(self):
        params = {
            "origin": "JFK",
            "destination": "LAX",
            "passengers": 1,
            "cabin": "ECONOMY",
            "nonstop": False,
        }
        offers = [
            {"price": {"grandTotal": "240.50", "currency": "USD"}, "itineraries": [{"segments": [{}]}]},
            {"price": {"grandTotal": "180.25", "currency": "USD"}, "itineraries": [{"segments": [{}]}]},
            {"price": {"grandTotal": "199.00", "currency": "USD"}, "itineraries": [{"segments": [{}]}]},
        ]

        with patch.object(flight_app.AMAD, "flight_offers_raw", return_value=offers) as raw_mock, \
             patch("app.search_flights") as search_mock:
            first = flight_app._light_cheapest_for_date(params, "2099-05-10", "2099-05-17")
            second = flight_app._light_cheapest_for_date(params, "2099-05-10", "2099-05-17")

        self.assertEqual(first["scan_price_total"], 180.25)
        self.assertEqual(first["scan_currency"], "USD")
        self.assertEqual(second, first)
        self.assertEqual(raw_mock.call_count, 1)
        search_mock.assert_not_called()

    def test_find_best_week_in_month_skips_phase_two_when_candidate_signal_is_strong(self):
        params = {
            "origin": "JFK",
            "destination": "LAX",
            "flex_month": "2099-05",
            "trip_length_days": 7,
            "passengers": 1,
            "cabin": "ECONOMY",
            "nonstop": False,
            "sort": "cheapest",
        }

        last_day = monthrange(2099, 5)[1]
        rows = []
        for day in range(1, last_day + 1):
            price = 200.0 + day
            if day == 11:
                price = 149.0
            rows.append({
                "departureDate": f"2099-05-{day:02d}",
                "returnDate": f"2099-05-{day + 7:02d}" if day <= 24 else f"2099-06-{day - 24:02d}",
                "price": {"total": f"{price:.2f}", "currency": "USD"},
            })

        def verify_candidate(_base_params, candidate):
            dep_date = candidate["depart_date"]
            return_date = candidate["return_date"]
            day = int(dep_date[-2:])
            price = 200.0 + day
            if day == 11:
                price = 149.0
            return {
                "depart_date": dep_date,
                "return_date": return_date,
                "scan_price_total": price,
                "scan_currency": "USD",
            }

        with patch.object(flight_app.AMAD, "search_flight_dates", return_value=rows), \
             patch("app._verify_candidate", side_effect=verify_candidate) as verify_mock, \
             patch("app._light_cheapest_for_date") as light_mock, \
             patch("app.search_flights", return_value=[{"id": "winner"}]):
            result = flight_app.find_best_week_in_month(params)

        self.assertIsNotNone(result)
        self.assertEqual(result["depart_date"], "2099-05-11")
        self.assertEqual(result["return_date"], "2099-05-18")
        self.assertEqual(result["offers"], [{"id": "winner"}])
        self.assertEqual(verify_mock.call_count, max(flight_app.CHEAPEST_VERIFY_TOP_N, flight_app.FLEX_CHALLENGER_POOL))
        light_mock.assert_not_called()

    def test_find_best_week_in_month_keeps_full_fallback_when_candidates_are_missing(self):
        params = {
            "origin": "JFK",
            "destination": "LAX",
            "flex_month": "2099-06",
            "trip_length_days": 7,
            "passengers": 1,
            "cabin": "ECONOMY",
            "nonstop": False,
            "sort": "cheapest",
        }

        def snapshot_for_pair(_base_params, dep_date, return_date):
            day = int(dep_date[-2:])
            return {
                "depart_date": dep_date,
                "return_date": return_date,
                "scan_price_total": 300.0 + day,
                "scan_currency": "USD",
            }

        with patch.object(flight_app.AMAD, "search_flight_dates", return_value=[]), \
             patch("app._verify_candidate") as verify_mock, \
             patch("app._light_cheapest_for_date", side_effect=snapshot_for_pair) as light_mock, \
             patch("app.search_flights", return_value=[{"id": "winner"}]):
            result = flight_app.find_best_week_in_month(params)

        self.assertIsNotNone(result)
        self.assertEqual(result["depart_date"], "2099-06-01")
        self.assertEqual(result["return_date"], "2099-06-08")
        expected_scan_count = monthrange(2099, 6)[1]
        if flight_app.FLIGHT_PROVIDER_EFFECTIVE == "duffel":
            expected_scan_count = min(flight_app.DUFFEL_FLEX_SCAN_LIMIT, expected_scan_count)
        self.assertEqual(light_mock.call_count, expected_scan_count)
        verify_mock.assert_not_called()

    def test_parse_ai_flex_request_preserves_explicit_oneway(self):
        class FakeResponse:
            text = json.dumps({
                "origin": "JFK",
                "destination": "LHR",
                "depart_date": None,
                "return_date": None,
                "passengers": 1,
                "cabin": "ECONOMY",
                "nonstop": False,
                "max_price": None,
                "sort": "cheapest",
            })

        class FakeModel:
            def generate_content(self, _prompt):
                return FakeResponse()

        with patch.object(flight_app, "model", FakeModel()):
            parsed = flight_app.parse_ai_flight_request("cheapest one way flight to london from nyc in 2099-07")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["search_mode"], "flex")
        self.assertEqual(parsed["trip_type"], "oneway")
        self.assertEqual(parsed["flex_month"], "2099-07")
        self.assertNotIn("trip_length_days", parsed)

    def test_parse_ai_flex_request_ignores_model_guessed_first_of_month_dates(self):
        class FakeResponse:
            text = json.dumps({
                "origin": "JFK",
                "destination": "SEA",
                "depart_date": "2099-06-01",
                "return_date": "2099-06-06",
                "passengers": 1,
                "cabin": "ECONOMY",
                "nonstop": False,
                "max_price": None,
                "sort": "cheapest",
            })

        class FakeModel:
            def generate_content(self, _prompt):
                return FakeResponse()

        with patch.object(flight_app, "model", FakeModel()):
            parsed = flight_app.parse_ai_flight_request("cheapest 5 day trip to seattle from jfk in june")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["search_mode"], "flex")
        self.assertEqual(parsed["flex_month"], flight_app._extract_ai_flex_month("in june"))
        self.assertEqual(parsed["trip_length_days"], 5)
        self.assertIsNone(parsed["depart_date"])
        self.assertIsNone(parsed["return_date"])

    def test_parse_ai_month_only_request_clears_inferred_exact_dates(self):
        class FakeResponse:
            text = json.dumps({
                "origin": "JFK",
                "destination": "SEA",
                "depart_date": "2099-07-01",
                "return_date": None,
                "passengers": 1,
                "cabin": "ECONOMY",
                "nonstop": False,
                "max_price": None,
                "sort": "recommended",
            })

        class FakeModel:
            def generate_content(self, _prompt):
                return FakeResponse()

        with patch.object(flight_app, "model", FakeModel()):
            parsed = flight_app.parse_ai_flight_request("find flights from jfk to seattle in july")

        self.assertIsNotNone(parsed)
        self.assertNotIn("search_mode", parsed)
        self.assertIsNone(parsed["depart_date"])
        self.assertIsNone(parsed["return_date"])

    def test_extract_ai_flex_month_supports_relative_months(self):
        today = flight_app.date.today()
        next_month_anchor = today.replace(day=28) + flight_app.timedelta(days=4)
        next_month = next_month_anchor.replace(day=1).strftime("%Y-%m")
        this_month = today.strftime("%Y-%m")

        self.assertEqual(flight_app._extract_ai_flex_month("cheapest fares next month"), next_month)
        self.assertEqual(flight_app._extract_ai_flex_month("flexible dates this month"), this_month)

    def test_parse_ai_manual_combination_request_sets_manual_mode(self):
        class FakeResponse:
            text = json.dumps({
                "origin": "JFK",
                "destination": "LAX",
                "depart_date": "2099-08-10",
                "return_date": "2099-08-17",
                "passengers": 1,
                "cabin": "ECONOMY",
                "nonstop": False,
                "max_price": None,
                "sort": "recommended",
            })

        class FakeModel:
            def generate_content(self, _prompt):
                return FakeResponse()

        with patch.object(flight_app, "model", FakeModel()):
            parsed = flight_app.parse_ai_flight_request(
                "Round trip from JFK to LAX on 2099-08-10 returning 2099-08-17. Let me choose the outbound and return separately."
            )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["combination_mode"], "manual")
        self.assertEqual(parsed["trip_type"], "roundtrip")

    def test_parse_ai_multicity_falls_back_to_text_route_chain_with_then_separator(self):
        class FakeResponse:
            text = json.dumps({
                "origin": None,
                "destination": None,
                "depart_date": None,
                "return_date": None,
                "legs": None,
                "trip_type": None,
                "passengers": 1,
                "cabin": "ECONOMY",
                "nonstop": False,
                "max_price": None,
                "sort": "recommended",
            })

        class FakeModel:
            def generate_content(self, _prompt):
                return FakeResponse()

        with patch.object(flight_app, "model", FakeModel()):
            parsed = flight_app.parse_ai_flight_request(
                "JFK to LHR then CDG then FCO"
            )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["trip_type"], "multicity")
        self.assertEqual(
            parsed["legs"],
            [
                {"origin": "JFK", "destination": "LHR", "depart_date": parsed["legs"][0]["depart_date"]},
                {"origin": "LHR", "destination": "CDG", "depart_date": parsed["legs"][1]["depart_date"]},
                {"origin": "CDG", "destination": "FCO", "depart_date": parsed["legs"][2]["depart_date"]},
            ],
        )
        self.assertEqual(parsed["origin"], "JFK")
        self.assertEqual(parsed["destination"], "FCO")
        self.assertIsNone(parsed["return_date"])

    def test_parse_ai_multicity_falls_back_to_text_route_chain_with_arrows(self):
        class FakeResponse:
            text = json.dumps({
                "origin": None,
                "destination": None,
                "depart_date": None,
                "return_date": None,
                "legs": [],
                "trip_type": None,
                "passengers": 1,
                "cabin": "ECONOMY",
                "nonstop": False,
                "max_price": None,
                "sort": "recommended",
            })

        class FakeModel:
            def generate_content(self, _prompt):
                return FakeResponse()

        with patch.object(flight_app, "model", FakeModel()):
            parsed = flight_app.parse_ai_flight_request("SFO -> JFK -> BOS")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["trip_type"], "multicity")
        self.assertEqual(
            [(leg["origin"], leg["destination"]) for leg in parsed["legs"]],
            [("SFO", "JFK"), ("JFK", "BOS")],
        )

    def test_find_best_oneway_day_in_month_picks_cheapest_departure(self):
        params = {
            "origin": "JFK",
            "destination": "LHR",
            "trip_type": "oneway",
            "flex_month": "2099-07",
            "passengers": 1,
            "cabin": "ECONOMY",
            "nonstop": False,
            "sort": "cheapest",
        }

        def snapshot_for_date(_base_params, dep_date):
            day = int(dep_date[-2:])
            price = 350.0 + day
            if day == 11:
                price = 199.0
            return {
                "depart_date": dep_date,
                "scan_price_total": price,
                "scan_currency": "USD",
            }

        with patch("app._light_cheapest_oneway_for_date", side_effect=snapshot_for_date) as light_mock, \
             patch("app.search_flights", return_value=[{"id": "winner"}]):
            result = flight_app.find_best_oneway_day_in_month(params)

        self.assertIsNotNone(result)
        self.assertEqual(result["depart_date"], "2099-07-11")
        self.assertEqual(result["offers"], [{"id": "winner"}])
        self.assertLess(light_mock.call_count, monthrange(2099, 7)[1])

    def test_format_flex_no_results_error_is_specific_and_mentions_test_data(self):
        params = {
            "origin": "JFK",
            "destination": "LHR",
            "flex_month": "2026-06",
            "trip_length_days": 15,
        }

        message = flight_app._format_flex_no_results_error(params)

        self.assertIn("15-day round-trip options", message)
        self.assertIn("JFK → LHR", message)
        self.assertIn("2026-06", message)
        if flight_app.FLIGHT_PROVIDER_EFFECTIVE == "amadeus" and flight_app.AMADEUS_ENV == "test":
            self.assertIn("Amadeus test data", message)

    def test_format_flex_no_results_error_handles_oneway(self):
        params = {
            "origin": "JFK",
            "destination": "LHR",
            "trip_type": "oneway",
            "flex_month": "2026-07",
        }

        message = flight_app._format_flex_no_results_error(params)

        self.assertIn("one-way options", message)
        self.assertNotIn("round-trip", message)


class HolidaySeasonInferenceTests(unittest.TestCase):
    def test_thanksgiving_returns_future_round_trip(self):
        from datetime import date

        anchor = date(2026, 4, 15)
        pair = flight_app._infer_holiday_season_round_trip(
            "JFK to LAX for Thanksgiving", anchor=anchor, parsed={}
        )
        self.assertIsNotNone(pair)
        d0, d1 = pair
        self.assertLess(d0, d1)
        self.assertTrue(d0.startswith("2026-11-"))

    def test_one_way_skips_holiday_defaults(self):
        from datetime import date

        pair = flight_app._infer_holiday_season_round_trip(
            "One way to London for Christmas", anchor=date(2026, 6, 1), parsed={}
        )
        self.assertIsNone(pair)

    def test_summer_uses_july_window(self):
        from datetime import date

        pair = flight_app._infer_holiday_season_round_trip(
            "NYC to SFO this summer", anchor=date(2026, 4, 15), parsed={}
        )
        self.assertIsNotNone(pair)
        self.assertEqual(pair[0], "2026-07-06")
        self.assertEqual(pair[1], "2026-07-13")

    def test_memorial_day_maps_to_long_weekend(self):
        from datetime import date

        pair = flight_app._infer_holiday_season_round_trip(
            "LAX to NYC for memorial day weekend", anchor=date(2026, 4, 1), parsed={}
        )
        self.assertIsNotNone(pair)
        self.assertEqual(pair[0], "2026-05-23")
        self.assertEqual(pair[1], "2026-05-25")

    def test_eu_origin_uses_eu_summer_window(self):
        from datetime import date

        pair = flight_app._infer_holiday_season_round_trip(
            "LHR to BCN in summer",
            anchor=date(2026, 4, 15),
            parsed={"origin": "LHR", "trip_type": "roundtrip"},
        )
        self.assertIsNotNone(pair)
        self.assertEqual(pair[0], "2026-07-25")
        self.assertEqual(pair[1], "2026-08-08")

    def test_continental_eu_origin_uses_late_summer_window(self):
        from datetime import date

        pair = flight_app._infer_holiday_season_round_trip(
            "CDG to BCN in summer",
            anchor=date(2026, 4, 15),
            parsed={"origin": "CDG", "trip_type": "roundtrip"},
        )
        self.assertIsNotNone(pair)
        self.assertEqual(pair[0], "2026-08-05")
        self.assertEqual(pair[1], "2026-08-19")


if __name__ == "__main__":
    unittest.main()
