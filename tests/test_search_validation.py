import unittest
from datetime import date, timedelta
from unittest.mock import patch

import app as flight_app


class SearchValidationTests(unittest.TestCase):
    def setUp(self):
        flight_app.app.config["TESTING"] = True
        self.client = flight_app.app.test_client()
        for cache_name in (
            "SEARCH_CACHE",
            "RAW_SEARCH_CACHE",
            "FLEX_RESULT_CACHE",
            "FLIGHT_DATES_CACHE",
            "CHEAPEST_SNAPSHOT_CACHE",
            "AMADEUS_FAILURE_CACHE",
        ):
            cache = getattr(flight_app, cache_name, None)
            if cache is None:
                continue
            cache.clear()

    def _future_date(self, days: int) -> str:
        return (date.today() + timedelta(days=days)).isoformat()

    def _future_month(self, months_ahead: int = 1) -> str:
        today = date.today().replace(day=1)
        month_index = today.month - 1 + months_ahead
        year = today.year + (month_index // 12)
        month = (month_index % 12) + 1
        return f"{year:04d}-{month:02d}"

    def _sample_roundtrip_flight(
        self,
        *,
        token: str,
        price: float,
        outbound_code: str,
        return_code: str,
        outbound_depart: str = "2099-06-01T08:00:00",
        outbound_arrive: str = "2099-06-01T11:00:00",
        return_depart: str = "2099-06-08T09:00:00",
        return_arrive: str = "2099-06-08T17:00:00",
    ) -> dict:
        return {
            "selection_token": token,
            "price": price,
            "price_per_pax": price,
            "currency": "USD",
            "booking_url": "https://example.com/book",
            "airline_summary": "Delta Air Lines",
            "out_airline": "Delta Air Lines",
            "out_airline_code": "DL",
            "out_depart_at": outbound_depart,
            "out_arrive_at": outbound_arrive,
            "out_stops": 0,
            "in_airline": "Delta Air Lines",
            "in_airline_code": "DL",
            "in_depart_at": return_depart,
            "in_arrive_at": return_arrive,
            "in_stops": 0,
            "segments_ui": [
                {
                    "label": "Outbound",
                    "airline": "Delta Air Lines",
                    "origin": "JFK",
                    "destination": "LAX",
                    "depart_time": "8:00 AM",
                    "depart_day": "Mon, Jun 1",
                    "arrive_time": "11:00 AM",
                    "arrive_day": "Mon, Jun 1",
                    "duration": "6h 0m",
                    "stops_label": "Nonstop",
                    "route_chip": outbound_code,
                    "time_chip": "Morning",
                    "layovers": [],
                },
                {
                    "label": "Return",
                    "airline": "Delta Air Lines",
                    "origin": "LAX",
                    "destination": "JFK",
                    "depart_time": "9:00 AM",
                    "depart_day": "Mon, Jun 8",
                    "arrive_time": "5:00 PM",
                    "arrive_day": "Mon, Jun 8",
                    "duration": "5h 0m",
                    "stops_label": "Nonstop",
                    "route_chip": return_code,
                    "time_chip": "Morning",
                    "layovers": [],
                },
            ],
        }

    def test_standard_roundtrip_requires_return_date(self):
        with patch("app.search_flights") as search_mock:
            response = self.client.post("/search", data={
                "mode": "standard",
                "origin": "JFK",
                "destination": "LAX",
                "trip_type": "roundtrip",
                "depart_date": self._future_date(10),
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Please provide a return date or switch to one-way.", response.data)
        search_mock.assert_not_called()

    def test_standard_rejects_same_airport_route(self):
        with patch("app.search_flights") as search_mock:
            response = self.client.post("/search", data={
                "mode": "standard",
                "origin": "JFK",
                "destination": "jfk",
                "trip_type": "oneway",
                "depart_date": self._future_date(12),
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Origin and destination can&#39;t be the same airport.", response.data)
        search_mock.assert_not_called()

    def test_standard_rejects_return_before_departure(self):
        with patch("app.search_flights") as search_mock:
            response = self.client.post("/search", data={
                "mode": "standard",
                "origin": "JFK",
                "destination": "LAX",
                "trip_type": "roundtrip",
                "depart_date": self._future_date(12),
                "return_date": self._future_date(10),
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Return date must be the same day or after departure.", response.data)
        search_mock.assert_not_called()

    def test_flex_rejects_past_month(self):
        past_month = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

        with patch("app.find_best_week_in_month") as flex_mock:
            response = self.client.post("/search", data={
                "mode": "flex",
                "origin": "JFK",
                "destination": "LAX",
                "flex_month": past_month,
                "trip_length_days": "7",
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Please choose the current month or a future month.", response.data)
        flex_mock.assert_not_called()

    def test_flex_rejects_manual_combination_mode(self):
        with patch("app.find_best_week_in_month") as flex_mock:
            response = self.client.post("/search", data={
                "mode": "flex",
                "origin": "JFK",
                "destination": "LAX",
                "flex_month": self._future_month(2),
                "trip_length_days": "7",
                "combination_mode": "manual",
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose-your-own flight combinations are only available for fixed-date round trips.", response.data)
        flex_mock.assert_not_called()

    def test_flex_shell_ai_does_not_parse_before_shell(self):
        with patch("app.parse_ai_flight_request") as parse_mock:
            response = self.client.post(
                "/search/flex-shell",
                data={"mode": "ai", "ai_text": "cheapest week jfk to sea in july 7 days"},
            )
        parse_mock.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"flexStreamBootForm", response.data)
        self.assertIn(b'name="ai_text"', response.data)

    def test_flex_stream_ai_not_flex_emits_handoff_ndjson(self):
        parsed = {
            "origin": "JFK",
            "destination": "LAX",
            "depart_date": self._future_date(30),
            "return_date": self._future_date(37),
            "trip_type": "roundtrip",
            "passengers": 1,
            "cabin": "ECONOMY",
            "nonstop": False,
            "sort": "recommended",
            "search_mode": "standard",
        }
        with patch("app.parse_ai_flight_request", return_value=parsed):
            response = self.client.post(
                "/search/flex-stream",
                data={"mode": "ai", "ai_text": "jfk to lax round trip next month"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/x-ndjson", (response.content_type or "").lower())
        self.assertIn(b"ai_standard_handoff", response.data)

    def test_ai_blank_prompt_is_blocked(self):
        with patch("app.parse_ai_flight_request") as parse_mock:
            response = self.client.post("/search", data={
                "mode": "ai",
                "ai_text": "",
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Please describe the trip before searching.", response.data)
        parse_mock.assert_not_called()

    def test_ai_roundtrip_requires_return_signal(self):
        parsed = {
            "origin": "JFK",
            "destination": "LAX",
            "depart_date": self._future_date(14),
            "return_date": None,
            "trip_type": "roundtrip",
            "passengers": 1,
            "cabin": "ECONOMY",
            "nonstop": False,
            "sort": "recommended",
        }

        with patch("app.parse_ai_flight_request", return_value=parsed), \
             patch("app.search_flights") as search_mock:
            response = self.client.post("/search", data={
                "mode": "ai",
                "ai_text": "round trip from jfk to lax on a future date",
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Please include a return date, or say how long you want to stay.", response.data)
        search_mock.assert_not_called()

    def test_standard_invalid_passenger_value_falls_back_safely(self):
        with patch("app.search_flights", return_value=[]) as search_mock:
            response = self.client.post("/search", data={
                "mode": "standard",
                "origin": "JFK",
                "destination": "LAX",
                "trip_type": "oneway",
                "depart_date": self._future_date(9),
                "passengers": "oops",
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No flights found.", response.data)
        params = search_mock.call_args.args[0]
        self.assertEqual(params["passengers"], 1)

    def test_ai_city_names_are_normalized_before_search(self):
        parsed = {
            "origin": "new york",
            "destination": "london",
            "depart_date": self._future_date(18),
            "return_date": None,
            "trip_type": "oneway",
            "passengers": 1,
            "cabin": "ECONOMY",
            "nonstop": False,
            "sort": "recommended",
        }

        with patch("app.parse_ai_flight_request", return_value=parsed), \
             patch("app.search_flights", return_value=[]) as search_mock:
            response = self.client.post("/search", data={
                "mode": "ai",
                "ai_text": "one way from new york to london on a future date",
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No flights found.", response.data)
        params = search_mock.call_args.args[0]
        self.assertEqual(params["origin"], "JFK")
        self.assertEqual(params["destination"], "LHR")

    def test_ai_month_only_prompt_requires_exact_dates_instead_of_first_of_month_guess(self):
        class FakeResponse:
            text = (
                '{"origin":"JFK","destination":"SEA","depart_date":"2099-07-01",'
                '"return_date":null,"passengers":1,"cabin":"ECONOMY","nonstop":false,'
                '"max_price":null,"sort":"recommended"}'
            )

        class FakeModel:
            def generate_content(self, _prompt, **_kwargs):
                return FakeResponse()

        with patch.object(flight_app, "model", FakeModel()), \
             patch("app.search_flights") as search_mock:
            response = self.client.post("/search", data={
                "mode": "ai",
                "ai_text": "find flights from jfk to seattle in july",
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Please include a departure date, or ask for a month like &#39;in July&#39; if you want a flexible search.", response.data)
        search_mock.assert_not_called()

    def test_manual_flex_oneway_uses_best_day_search(self):
        best = {
            "depart_date": self._future_date(25),
            "scan_price_total": 199.0,
            "scan_currency": "USD",
            "offers": [],
        }

        with patch("app.find_best_oneway_day_in_month", return_value=best) as oneway_mock, \
             patch("app.find_best_week_in_month") as roundtrip_mock:
            response = self.client.post("/search", data={
                "mode": "flex",
                "origin": "JFK",
                "destination": "LAX",
                "trip_type": "oneway",
                "flex_month": self._future_month(2),
                "trip_length_days": "7",
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Best day found", response.data)
        params = oneway_mock.call_args.args[0]
        self.assertEqual(params["trip_type"], "oneway")
        self.assertNotIn("trip_length_days", params)
        roundtrip_mock.assert_not_called()

    def test_manual_combination_roundtrip_shows_outbound_selection_step(self):
        cheapest = self._sample_roundtrip_flight(token="rt-1", price=250.0, outbound_code="Direct route", return_code="Direct route")
        alternate = self._sample_roundtrip_flight(
            token="rt-2",
            price=310.0,
            outbound_code="Via ORD",
            return_code="Direct route",
            outbound_depart="2099-06-01T10:00:00",
            outbound_arrive="2099-06-01T15:00:00",
        )

        with patch("app.search_flights", return_value=[cheapest, alternate]) as search_mock:
            response = self.client.post("/search", data={
                "mode": "standard",
                "origin": "JFK",
                "destination": "LAX",
                "trip_type": "roundtrip",
                "depart_date": self._future_date(14),
                "return_date": self._future_date(21),
                "combination_mode": "manual",
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose your departure flight", response.data)
        self.assertIn(b"Choose outbound", response.data)
        self.assertIn(b"Total trip", response.data)
        self.assertNotIn(b"+$60.00", response.data)
        self.assertNotIn(b">$0<", response.data)
        self.assertEqual(search_mock.call_count, 1)

    def test_manual_combination_roundtrip_shows_return_deltas_after_outbound_pick(self):
        cheapest = self._sample_roundtrip_flight(token="rt-1", price=250.0, outbound_code="Direct route", return_code="Direct route")
        plus = self._sample_roundtrip_flight(
            token="rt-2",
            price=300.0,
            outbound_code="Direct route",
            return_code="Via DEN",
            return_depart="2099-06-08T12:00:00",
            return_arrive="2099-06-08T20:30:00",
        )

        with patch("app.search_flights", return_value=[cheapest, plus]) as search_mock:
            response = self.client.post("/search", data={
                "mode": "standard",
                "origin": "JFK",
                "destination": "LAX",
                "trip_type": "roundtrip",
                "depart_date": self._future_date(14),
                "return_date": self._future_date(21),
                "combination_mode": "manual",
                "selected_outbound_token": "rt-1",
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose your return flight", response.data)
        self.assertIn(b"Choose return", response.data)
        self.assertIn(b"USD $250.00", response.data)
        self.assertIn(b"+$50.00", response.data)
        self.assertNotIn(b">$0<", response.data)
        self.assertNotIn(b"Continue to Booking", response.data)
        self.assertNotIn(b"Book now", response.data)
        self.assertEqual(search_mock.call_count, 1)

    def test_ai_manual_combination_is_blocked_for_flex_requests(self):
        parsed = {
            "origin": "JFK",
            "destination": "LAX",
            "trip_type": "roundtrip",
            "search_mode": "flex",
            "flex_month": self._future_month(3),
            "trip_length_days": 7,
            "passengers": 1,
            "cabin": "ECONOMY",
            "nonstop": False,
            "sort": "cheapest",
            "combination_mode": "manual",
        }

        with patch("app.parse_ai_flight_request", return_value=parsed), \
             patch("app.find_best_week_in_month") as flex_mock:
            response = self.client.post("/search", data={
                "mode": "ai",
                "ai_text": "Let me choose the outbound and return separately for a 7 day trip in next month",
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choosing flights separately is only available for fixed-date round trips right now.", response.data)
        flex_mock.assert_not_called()

    def test_raw_offer_failures_are_not_cached_as_empty_results(self):
        class DummyResponse:
            ok = True
            status_code = 200
            text = ""

            def json(self):
                return {"data": [{"id": "offer-1"}]}

        client = flight_app.AmadeusClient()
        query = {
            "originLocationCode": "JFK",
            "destinationLocationCode": "LAX",
            "departureDate": self._future_date(20),
            "adults": 1,
            "travelClass": "ECONOMY",
            "nonStop": "false",
            "max": 24,
            "currencyCode": "USD",
        }

        with patch.object(
            client,
            "get",
            side_effect=[flight_app.requests.RequestException("boom"), DummyResponse()],
        ) as get_mock:
            self.assertIsNone(client.flight_offers_raw(query))
            self.assertIsNone(client.flight_offers_raw(query))
            flight_app.AMADEUS_FAILURE_CACHE.clear()
            self.assertEqual(client.flight_offers_raw(query), [{"id": "offer-1"}])

        self.assertEqual(get_mock.call_count, 2)

    def test_duffel_offer_normalization_preserves_existing_offer_shape(self):
        class DummyResponse:
            ok = True
            status_code = 201
            text = ""

            def json(self):
                return {
                    "data": {
                        "passengers": [{"id": "pas_1"}],
                        "offers": [
                            {
                                "id": "off_123",
                                "total_amount": "321.45",
                                "total_currency": "USD",
                                "slices": [
                                    {
                                        "duration": "PT6H10M",
                                        "segments": [
                                            {
                                                "origin": {"iata_code": "JFK"},
                                                "destination": {"iata_code": "LAX"},
                                                "departing_at": "2099-06-01T08:00:00",
                                                "arriving_at": "2099-06-01T11:10:00",
                                                "marketing_carrier": {"iata_code": "DL"},
                                                "operating_carrier": {"iata_code": "DL"},
                                                "marketing_carrier_flight_number": "123",
                                                "operating_carrier_flight_number": "123",
                                            }
                                        ],
                                    },
                                    {
                                        "duration": "PT5H30M",
                                        "segments": [
                                            {
                                                "origin": {"iata_code": "LAX"},
                                                "destination": {"iata_code": "JFK"},
                                                "departing_at": "2099-06-08T09:00:00",
                                                "arriving_at": "2099-06-08T17:30:00",
                                                "marketing_carrier": {"iata_code": "DL"},
                                                "operating_carrier": {"iata_code": "DL"},
                                                "marketing_carrier_flight_number": "456",
                                                "operating_carrier_flight_number": "456",
                                            }
                                        ],
                                    },
                                ],
                            }
                        ],
                    }
                }

        client = flight_app.DuffelClient()
        query = {
            "originLocationCode": "JFK",
            "destinationLocationCode": "LAX",
            "departureDate": self._future_date(20),
            "returnDate": self._future_date(27),
            "adults": 1,
            "travelClass": "ECONOMY",
            "nonStop": "false",
            "max": 10,
            "currencyCode": "USD",
        }

        with patch.object(client.session, "post", return_value=DummyResponse()) as post_mock:
            offers = client.flight_offers_raw(query, timeout=12, fast=False)

        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["price"]["grandTotal"], "321.45")
        self.assertEqual(offers[0]["price"]["currency"], "USD")
        self.assertEqual(len(offers[0]["itineraries"]), 2)
        self.assertEqual(
            offers[0]["itineraries"][0]["segments"][0]["departure"]["iataCode"],
            "JFK",
        )
        self.assertEqual(
            offers[0]["itineraries"][1]["segments"][0]["arrival"]["iataCode"],
            "JFK",
        )
        self.assertEqual(
            post_mock.call_args.kwargs["params"]["return_offers"],
            "true",
        )

    def test_search_flights_uses_single_fast_live_fetch(self):
        params = {
            "origin": "JFK",
            "destination": "LAX",
            "depart_date": self._future_date(16),
            "return_date": self._future_date(23),
            "trip_type": "roundtrip",
            "passengers": 1,
            "cabin": "ECONOMY",
            "nonstop": False,
            "sort": "cheapest",
        }
        parsed_flight = {
            "price": 240.0,
            "_sort_total_duration": 360,
            "out_stops": 0,
            "in_stops": 0,
        }

        with patch("app.AMAD.flight_offers_raw", return_value=[{"id": "primary"}]) as raw_mock, \
             patch("app._collect_best_presentations", return_value=[dict(parsed_flight)]), \
             patch("app._decorate_flights_for_display", side_effect=lambda flights, _params: flights), \
             patch("app._clean_flights_for_render", side_effect=lambda flights: flights), \
             patch("app._assign_smart_badges"):
            flights = flight_app.search_flights(params, detailed=True)

        self.assertEqual(len(flights), 1)
        self.assertEqual(raw_mock.call_count, 1)
        self.assertEqual(raw_mock.call_args.args[0]["max"], flight_app.SEARCH_RESULTS_FETCH_LIMIT)
        self.assertEqual(raw_mock.call_args.kwargs["timeout"], flight_app.LIVE_SEARCH_TIMEOUT)
        self.assertTrue(raw_mock.call_args.kwargs["fast"])
        self.assertFalse(raw_mock.call_args.kwargs["cache_failures"])

    def test_flex_none_result_is_cached_and_not_recomputed(self):
        params = {
            "origin": "JFK",
            "destination": "LAX",
            "trip_type": "roundtrip",
            "flex_month": self._future_month(2),
            "trip_length_days": 7,
            "passengers": 1,
            "cabin": "ECONOMY",
            "nonstop": False,
            "sort": "cheapest",
        }

        with patch("app.AMAD.search_flight_dates", return_value=[]), \
             patch("app._light_cheapest_for_date", return_value=None) as scan_mock:
            self.assertIsNone(flight_app.find_best_week_in_month(params))
            first_call_count = scan_mock.call_count
            self.assertGreater(first_call_count, 0)
            self.assertIsNone(flight_app.find_best_week_in_month(params))

        self.assertEqual(scan_mock.call_count, first_call_count)

    def test_standard_search_surfaces_provider_error_when_live_api_is_unavailable(self):
        flight_app._mark_amadeus_failure(
            "/v2/shopping/flight-offers",
            status_code=500,
            detail="internal error",
        )

        with patch("app.search_flights", return_value=[]):
            response = self.client.post("/search", data={
                "mode": "standard",
                "origin": "JFK",
                "destination": "LAX",
                "trip_type": "oneway",
                "depart_date": self._future_date(9),
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Live flight search is temporarily unavailable", response.data)

    def test_multicity_requires_at_least_two_legs(self):
        with patch("app.search_flights") as search_mock:
            response = self.client.post("/search", data={
                "mode": "standard",
                "trip_type": "multicity",
                "leg_origin": ["JFK"],
                "leg_destination": ["LAX"],
                "leg_date": [self._future_date(7)],
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Please include at least 2 legs for a multi-city trip.", response.data)
        search_mock.assert_not_called()

    def test_multicity_accepts_valid_legs_and_routes_search(self):
        with patch("app.search_flights", return_value=[]) as search_mock:
            response = self.client.post("/search", data={
                "mode": "standard",
                "trip_type": "multicity",
                "leg_origin": ["JFK", "LAX"],
                "leg_destination": ["LAX", "SFO"],
                "leg_date": [self._future_date(7), self._future_date(10)],
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No flights found.", response.data)
        params = search_mock.call_args.args[0]
        self.assertEqual(params["trip_type"], "multicity")
        self.assertEqual(len(params.get("legs") or []), 2)
        self.assertEqual(params["origin"], "JFK")
        self.assertEqual(params["destination"], "SFO")

    def test_multicity_infers_missing_next_origin_from_previous_destination(self):
        with patch("app.search_flights", return_value=[]) as search_mock:
            response = self.client.post("/search", data={
                "mode": "standard",
                "trip_type": "multicity",
                "leg_origin": ["JFK", ""],
                "leg_destination": ["LAX", "SFO"],
                "leg_date": [self._future_date(7), self._future_date(10)],
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No flights found.", response.data)
        params = search_mock.call_args.args[0]
        self.assertEqual(
            params["legs"],
            [
                {"origin": "JFK", "destination": "LAX", "depart_date": self._future_date(7)},
                {"origin": "LAX", "destination": "SFO", "depart_date": self._future_date(10)},
            ],
        )

    def test_results_edit_search_multicity_post_is_accepted(self):
        with patch("app.search_flights", return_value=[]) as search_mock:
            response = self.client.post("/search", data={
                "mode": "standard",
                "trip_type": "multicity",
                "cabin": "ECONOMY",
                "passengers": "1",
                "sort": "recommended",
                "nonstop": "",
                "leg_origin": ["JFK", "LAX"],
                "leg_destination": ["LAX", "SFO"],
                "leg_date": [self._future_date(7), self._future_date(10)],
            })
        self.assertEqual(response.status_code, 200)
        params = search_mock.call_args.args[0]
        self.assertEqual(params["trip_type"], "multicity")
        self.assertEqual(params["origin"], "JFK")
        self.assertEqual(params["destination"], "SFO")
        self.assertEqual(len(params.get("legs") or []), 2)


if __name__ == "__main__":
    unittest.main()
