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

    def test_on_thanksgiving_day_with_next_day_return_is_honored(self):
        from datetime import date

        anchor = date(2026, 7, 22)
        pair = flight_app._infer_holiday_season_round_trip(
            "I want to fly to detroit from nyc on Thanksgiving day and come back the next day",
            anchor=anchor,
            parsed={},
        )
        self.assertIsNotNone(pair)
        thanksgiving = flight_app._us_thanksgiving(2026).isoformat()
        self.assertEqual(pair[0], thanksgiving)
        self.assertEqual(pair[1], "2026-11-27")

    def test_default_thanksgiving_window_unaffected_without_explicit_pin(self):
        from datetime import date

        pair = flight_app._infer_holiday_season_round_trip(
            "JFK to LAX for Thanksgiving", anchor=date(2026, 7, 22), parsed={}
        )
        self.assertEqual(pair, ("2026-11-25", "2026-11-29"))

    def test_same_day_return_collapses_window_to_single_day(self):
        from datetime import date

        pair = flight_app._infer_holiday_season_round_trip(
            "flights for christmas, same day return", anchor=date(2026, 7, 22), parsed={}
        )
        self.assertIsNotNone(pair)
        self.assertEqual(pair[0], pair[1])

    def test_explicit_days_later_return_overrides_default_window(self):
        from datetime import date

        pair = flight_app._infer_holiday_season_round_trip(
            "trip for diwali, returning 5 days later", anchor=date(2026, 7, 22), parsed={}
        )
        self.assertIsNotNone(pair)
        d0 = flight_app._to_date(pair[0])
        d1 = flight_app._to_date(pair[1])
        self.assertEqual((d1 - d0).days, 5)

    def test_lunar_new_year_is_not_shadowed_by_generic_new_year_match(self):
        from datetime import date

        pair = flight_app._infer_holiday_season_round_trip(
            "flights to seoul for lunar new year", anchor=date(2026, 7, 22), parsed={}
        )
        plain_new_year = flight_app._infer_holiday_season_round_trip(
            "flights for new years", anchor=date(2026, 7, 22), parsed={}
        )
        self.assertIsNotNone(pair)
        self.assertNotEqual(pair, plain_new_year)

    def test_eid_al_adha_is_not_shadowed_by_bare_eid_match(self):
        from datetime import date

        adha_pair = flight_app._infer_holiday_season_round_trip(
            "flights to mecca for eid al adha", anchor=date(2026, 7, 22), parsed={}
        )
        fitr_pair = flight_app._infer_holiday_season_round_trip(
            "flights to istanbul for eid", anchor=date(2026, 7, 22), parsed={}
        )
        self.assertIsNotNone(adha_pair)
        self.assertIsNotNone(fitr_pair)
        self.assertNotEqual(adha_pair, fitr_pair)

    def test_hanukkah_returns_a_future_window(self):
        from datetime import date

        pair = flight_app._infer_holiday_season_round_trip(
            "flights to tel aviv for hanukkah", anchor=date(2026, 7, 22), parsed={}
        )
        self.assertIsNotNone(pair)
        d0, d1 = pair
        self.assertLess(d0, d1)

    def test_explicit_calendar_dates_are_not_overridden_by_holiday_keyword(self):
        # If the model already extracted real dates (day precision present in
        # the text), the holiday keyword shouldn't silently clobber them.
        class FakeResponse:
            text = json.dumps({
                "origin": "JFK",
                "destination": "LAX",
                "depart_date": "2026-11-20",
                "return_date": "2026-11-27",
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
                "JFK to LAX, Thanksgiving week, November 20 to November 27"
            )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["depart_date"], "2026-11-20")
        self.assertEqual(parsed["return_date"], "2026-11-27")


class TravelerContextTests(unittest.TestCase):
    def test_detects_companion_and_longer_layover_preference(self):
        context = flight_app._extract_ai_traveler_context(
            "I want to fly with my mom, prefer a longer layover"
        )
        self.assertIn("mom", context["companion_labels"])
        self.assertTrue(context["has_senior_or_child_companion"])
        self.assertTrue(context["prefers_longer_layover"])
        self.assertFalse(context["prefers_shorter_layover"])

    def test_detects_shorter_layover_preference_with_kids(self):
        context = flight_app._extract_ai_traveler_context(
            "traveling with my kids, want a short layover"
        )
        self.assertIn("kids", context["companion_labels"])
        self.assertTrue(context["prefers_shorter_layover"])

    def test_solo_traveler_has_no_companion_context(self):
        context = flight_app._extract_ai_traveler_context("just me flying to austin")
        self.assertEqual(context["companion_labels"], [])
        self.assertFalse(context["has_senior_or_child_companion"])

    def test_parse_ai_flight_request_attaches_traveler_context(self):
        class FakeResponse:
            text = json.dumps({
                "origin": "JFK",
                "destination": "LAX",
                "depart_date": "2099-08-10",
                "return_date": "2099-08-17",
                "passengers": 2,
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
                "JFK to LAX Aug 10 to Aug 17, flying with my mom, prefer a longer layover"
            )

        self.assertIsNotNone(parsed)
        self.assertIn("traveler_context", parsed)
        self.assertIn("mom", parsed["traveler_context"]["companion_labels"])
        self.assertTrue(parsed["traveler_context"]["prefers_longer_layover"])


class SmartBadgeReasoningTests(unittest.TestCase):
    def test_top_pick_reasoning_is_dynamic_not_generic(self):
        flights = [
            {"price": 420.0, "_sort_total_duration": 300, "out_stops": 0, "in_stops": 0,
             "_airline_key": "AA", "out_layovers": [], "in_layovers": []},
            {"price": 380.0, "_sort_total_duration": 340, "out_stops": 1, "in_stops": 1,
             "_airline_key": "AA", "out_layovers": [{"minutes": 95}], "in_layovers": [{"minutes": 100}]},
            {"price": 500.0, "_sort_total_duration": 280, "out_stops": 0, "in_stops": 0,
             "_airline_key": "DL", "out_layovers": [], "in_layovers": []},
        ]
        params = {"max_price": 450, "nonstop": False}
        flight_app._assign_smart_badges(flights, "recommended", params=params)

        reasoning = flights[0]["badge_reasoning"]
        self.assertNotEqual(reasoning, "Optimized for price, duration, and timing")
        self.assertIn("budget", reasoning.lower())

    def test_top_pick_reasoning_mentions_companion_context(self):
        flights = [
            {"price": 400.0, "_sort_total_duration": 400, "out_stops": 1, "in_stops": 1,
             "_airline_key": "AA", "out_layovers": [{"minutes": 100}], "in_layovers": [{"minutes": 90}]},
            {"price": 400.0, "_sort_total_duration": 400, "out_stops": 1, "in_stops": 1,
             "_airline_key": "DL", "out_layovers": [{"minutes": 300}], "in_layovers": [{"minutes": 300}]},
        ]
        params = {
            "traveler_context": {
                "companion_labels": ["kids"],
                "has_senior_or_child_companion": True,
                "prefers_shorter_layover": True,
                "prefers_longer_layover": False,
            }
        }
        flight_app._assign_smart_badges(flights, "recommended", params=params)
        self.assertIn("kids", flights[0]["badge_reasoning"].lower())

    def test_top_pick_reasoning_falls_back_when_no_signal(self):
        # flights[0] is deliberately the pricier, slower, connecting option so
        # none of the "why this one" signals fire and the generic fallback
        # copy is used instead.
        flights = [
            {"price": 500.0, "_sort_total_duration": 400, "out_stops": 1, "in_stops": 1,
             "_airline_key": "AA", "out_layovers": [{"minutes": 90}], "in_layovers": [{"minutes": 90}]},
            {"price": 300.0, "_sort_total_duration": 300, "out_stops": 0, "in_stops": 0,
             "_airline_key": "DL", "out_layovers": [], "in_layovers": []},
        ]
        flight_app._assign_smart_badges(flights, "recommended", params=None)
        self.assertEqual(
            flights[0]["badge_reasoning"],
            "Best overall balance of price, duration, and flight timing for this search",
        )


if __name__ == "__main__":
    unittest.main()
