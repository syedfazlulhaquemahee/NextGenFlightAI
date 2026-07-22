import unittest
from unittest.mock import patch

from app import (
    _decorate_flights_for_display,
    _diversify_recommended_flights,
    _expand_recommended_offers_if_needed,
    _parse_offer,
    _prefer_display_offer,
    _presentation_signature,
    _rebalance_recommended_flights,
    _recommended_results_are_too_narrow,
)


class MarketplaceResultsTests(unittest.TestCase):
    @staticmethod
    def _offer(carrier: str, number: str, depart_hour: int) -> dict:
        depart = f"2026-06-01T{depart_hour:02d}:00:00"
        arrive = f"2026-06-01T{depart_hour + 3:02d}:00:00"
        return {
            "price": {"grandTotal": "250.00", "currency": "USD"},
            "travelerPricings": [{}],
            "itineraries": [
                {
                    "duration": "PT3H",
                    "segments": [
                        {
                            "carrierCode": carrier,
                            "number": number,
                            "departure": {"iataCode": "JFK", "at": depart},
                            "arrival": {"iataCode": "LAX", "at": arrive},
                        }
                    ],
                }
            ],
        }

    def test_recommended_diversification_promotes_other_airlines(self):
        flights = [
            {"_airline_key": "AA", "_recommended_score": 100.0, "price": 210.0, "_sort_total_duration": 360},
            {"_airline_key": "AA", "_recommended_score": 98.0, "price": 220.0, "_sort_total_duration": 365},
            {"_airline_key": "AA", "_recommended_score": 96.0, "price": 225.0, "_sort_total_duration": 370},
            {"_airline_key": "DL", "_recommended_score": 95.0, "price": 230.0, "_sort_total_duration": 355},
            {"_airline_key": "UA", "_recommended_score": 94.0, "price": 235.0, "_sort_total_duration": 350},
        ]

        diversified = _diversify_recommended_flights([dict(flight) for flight in flights])

        self.assertEqual(
            [flight["_airline_key"] for flight in diversified[:3]],
            ["AA", "DL", "UA"],
        )

    def test_rebalance_limits_how_many_top_results_one_airline_can_take(self):
        flights = [
            {"_airline_key": "AA", "_recommended_score": 120.0 - idx, "price": 200.0 + idx, "_sort_total_duration": 300 + idx}
            for idx in range(12)
        ] + [
            {"_airline_key": "DL", "_recommended_score": 98.0, "price": 230.0, "_sort_total_duration": 320},
            {"_airline_key": "DL", "_recommended_score": 97.5, "price": 231.0, "_sort_total_duration": 321},
            {"_airline_key": "UA", "_recommended_score": 97.0, "price": 235.0, "_sort_total_duration": 325},
            {"_airline_key": "UA", "_recommended_score": 96.5, "price": 236.0, "_sort_total_duration": 326},
            {"_airline_key": "B6", "_recommended_score": 96.0, "price": 240.0, "_sort_total_duration": 330},
            {"_airline_key": "B6", "_recommended_score": 95.5, "price": 241.0, "_sort_total_duration": 331},
            {"_airline_key": "WN", "_recommended_score": 95.0, "price": 245.0, "_sort_total_duration": 335},
            {"_airline_key": "WN", "_recommended_score": 94.5, "price": 246.0, "_sort_total_duration": 336},
        ]

        rebalanced = _rebalance_recommended_flights(flights, limit=10)

        self.assertEqual(
            [flight["_airline_key"] for flight in rebalanced[:5]],
            ["AA", "AA", "AA", "DL", "DL"],
        )
        self.assertLessEqual(
            sum(1 for flight in rebalanced[:10] if flight["_airline_key"] == "AA"),
            3,
        )

    def test_narrowness_check_skips_small_result_sets(self):
        flights = [
            {"_airline_key": "AA"} for _ in range(8)
        ]

        self.assertFalse(_recommended_results_are_too_narrow(flights))

    @patch("app.AMAD.flight_offers_raw")
    def test_recommended_pool_fetches_one_fast_alt_query_when_results_are_truly_narrow(self, mock_flight_offers_raw):
        base_offers = [
            self._offer("AA", f"{100 + idx}", idx)
            for idx in range(1, 9)
        ] + [
            self._offer("DL", "301", 12),
            self._offer("UA", "401", 13),
        ]
        alt_offers = [
            self._offer("B6", "501", 14),
            self._offer("WN", "601", 15),
            self._offer("AS", "701", 16),
            self._offer("NK", "801", 17),
            self._offer("F9", "901", 18),
        ]
        mock_flight_offers_raw.return_value = alt_offers

        ranked_flights = [
            {"_airline_key": "AA"} for _ in range(16)
        ] + [
            {"_airline_key": "DL"} for _ in range(2)
        ] + [
            {"_airline_key": "UA"} for _ in range(2)
        ]

        offers = _expand_recommended_offers_if_needed(
            {
                "origin": "JFK",
                "destination": "LAX",
                "depart_date": "2026-06-01",
                "return_date": None,
                "passengers": 1,
                "cabin": "ECONOMY",
                "nonstop": False,
                "sort": "recommended",
            },
            base_offers,
            ranked_flights,
            detailed=True,
        )

        self.assertEqual(mock_flight_offers_raw.call_count, 1)
        query = mock_flight_offers_raw.call_args.args[0]
        kwargs = mock_flight_offers_raw.call_args.kwargs
        self.assertEqual(query.get("excludedAirlineCodes"), "AA")
        self.assertEqual(query.get("max"), 20)
        self.assertTrue(kwargs.get("fast"))
        carriers = {offer["itineraries"][0]["segments"][0]["carrierCode"] for offer in offers}
        self.assertEqual(carriers, {"AA", "DL", "UA", "B6", "WN", "AS", "NK", "F9"})

    def test_display_signature_prefers_cheaper_version_of_same_schedule(self):
        cheaper = {
            "price": 199.0,
            "_sort_total_duration": 320,
            "out_depart_at": "2026-06-01T08:00:00",
            "out_arrive_at": "2026-06-01T10:30:00",
            "_out_via_codes": ["ATL"],
            "out_stops": 1,
            "in_depart_at": "2026-06-08T13:00:00",
            "in_arrive_at": "2026-06-08T18:15:00",
            "_in_via_codes": [],
            "in_stops": 0,
        }
        pricier = dict(cheaper, price=249.0)

        self.assertEqual(_presentation_signature(cheaper), _presentation_signature(pricier))
        self.assertTrue(_prefer_display_offer(cheaper, pricier))
        self.assertFalse(_prefer_display_offer(pricier, cheaper))

    def test_parse_offer_builds_full_airline_summary(self):
        offer = {
            "price": {"grandTotal": "525.00", "currency": "USD"},
            "travelerPricings": [{}, {}],
            "itineraries": [
                {
                    "duration": "PT6H30M",
                    "segments": [
                        {
                            "carrierCode": "AA",
                            "number": "101",
                            "departure": {"iataCode": "JFK", "at": "2026-06-01T08:00:00"},
                            "arrival": {"iataCode": "ORD", "at": "2026-06-01T10:00:00"},
                        },
                        {
                            "carrierCode": "AA",
                            "number": "202",
                            "departure": {"iataCode": "ORD", "at": "2026-06-01T11:15:00"},
                            "arrival": {"iataCode": "LAX", "at": "2026-06-01T14:30:00"},
                        },
                    ],
                },
                {
                    "duration": "PT5H45M",
                    "segments": [
                        {
                            "carrierCode": "DL",
                            "number": "303",
                            "departure": {"iataCode": "LAX", "at": "2026-06-08T09:00:00"},
                            "arrival": {"iataCode": "JFK", "at": "2026-06-08T16:45:00"},
                        }
                    ],
                },
            ],
        }
        params = {
            "origin": "JFK",
            "destination": "LAX",
            "depart_date": "2026-06-01",
            "return_date": "2026-06-08",
            "passengers": 2,
            "cabin": "ECONOMY",
            "nonstop": False,
        }

        parsed = _parse_offer(offer, params, detailed=True)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["airline_summary"], "American Airlines + Delta Air Lines")
        self.assertEqual(parsed["out_airline"], "American Airlines")
        self.assertEqual(parsed["in_airline"], "Delta Air Lines")
        self.assertIn("Outbound American Airlines", parsed["airline_mix_label"])

    def test_parse_offer_prefers_supplier_duration_for_cross_timezone_segments(self):
        dl_logo = "https://assets.duffel.com/img/airlines/for-light-background/full-color-logo/DL.svg"
        offer = {
            "id": "off_cross_timezone",
            "total_amount": "321.45",
            "total_currency": "USD",
            "passengers": [{"id": "pas_1"}],
            "owner": {
                "name": "Delta Air Lines",
                "iata_code": "DL",
                "logo_symbol_url": dl_logo,
            },
            "slices": [
                {
                    "duration": "PT6H10M",
                    "segments": [
                        {
                            "origin": {"iata_code": "JFK"},
                            "destination": {"iata_code": "LAX"},
                            "departing_at": "2099-06-01T08:00:00",
                            "arriving_at": "2099-06-01T11:10:00",
                            "marketing_carrier": {
                                "iata_code": "DL",
                                "logo_symbol_url": dl_logo,
                            },
                            "operating_carrier": {"iata_code": "DL"},
                            "marketing_carrier_flight_number": "123",
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
                            "marketing_carrier": {
                                "iata_code": "DL",
                                "logo_symbol_url": dl_logo,
                            },
                            "operating_carrier": {"iata_code": "DL"},
                            "marketing_carrier_flight_number": "456",
                        }
                    ],
                },
            ],
        }
        params = {
            "origin": "JFK",
            "destination": "LAX",
            "depart_date": "2099-06-01",
            "return_date": "2099-06-08",
            "passengers": 1,
            "cabin": "ECONOMY",
            "nonstop": False,
        }

        parsed = _parse_offer(offer, params, detailed=True)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["airline_logo_url"], dl_logo)
        self.assertEqual(parsed["out_airline_logo_url"], dl_logo)
        self.assertEqual(parsed["in_airline_logo_url"], dl_logo)
        self.assertEqual(parsed["out_duration_min"], 370)
        self.assertEqual(parsed["in_duration_min"], 330)

        decorated = _decorate_flights_for_display([parsed], params)[0]
        self.assertEqual(decorated["segments_ui"][0]["airline_logo_url"], dl_logo)
        self.assertEqual(decorated["segments_ui"][1]["airline_logo_url"], dl_logo)
        self.assertEqual(decorated["segments_ui"][0]["depart_time"], "8:00 AM")
        self.assertEqual(decorated["segments_ui"][0]["arrive_time"], "11:10 AM")
        self.assertEqual(decorated["segments_ui"][0]["duration"], "6h 10m")
        self.assertEqual(decorated["segments_ui"][1]["duration"], "5h 30m")

    def test_parse_offer_raises_impossibly_short_slice_duration_to_segment_plus_layover_floor(self):
        offer = {
            "id": "off_bad_slice_duration",
            "total_amount": "654.00",
            "total_currency": "USD",
            "passengers": [{"id": "pas_1"}],
            "owner": {"name": "Example Air", "iata_code": "EA"},
            "slices": [
                {
                    "duration": "PT1H55M",
                    "segments": [
                        {
                            "origin": {"iata_code": "JFK"},
                            "destination": {"iata_code": "BOS"},
                            "departing_at": "2099-05-06T22:30:00-04:00",
                            "arriving_at": "2099-05-06T23:55:00-04:00",
                            "marketing_carrier": {"iata_code": "EA"},
                            "operating_carrier": {"iata_code": "EA"},
                            "marketing_carrier_flight_number": "101",
                        },
                        {
                            "origin": {"iata_code": "BOS"},
                            "destination": {"iata_code": "SNN"},
                            "departing_at": "2099-05-07T18:24:00-04:00",
                            "arriving_at": "2099-05-08T05:25:00+01:00",
                            "marketing_carrier": {"iata_code": "EA"},
                            "operating_carrier": {"iata_code": "EA"},
                            "marketing_carrier_flight_number": "202",
                        },
                    ],
                }
            ],
        }
        params = {
            "origin": "JFK",
            "destination": "SNN",
            "depart_date": "2099-05-06",
            "return_date": None,
            "trip_type": "oneway",
            "passengers": 1,
            "cabin": "ECONOMY",
            "nonstop": False,
        }

        parsed = _parse_offer(offer, params, detailed=True)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["out_duration_min"], 1555)

        decorated = _decorate_flights_for_display([parsed], params)[0]
        self.assertEqual(decorated["segments_ui"][0]["duration"], "25h 55m")
        self.assertEqual(decorated["segments_ui"][0]["layovers"][0]["duration_label"], "18h 29m")

    def test_display_decoration_adds_time_and_layover_chips(self):
        flights = [{
            "airline_mix_label": "Same airline both ways",
            "out_airline": "American Airlines",
            "out_airline_code": "AA",
            "out_depart_at": "2026-06-01T05:30:00",
            "out_arrive_at": "2026-06-01T10:30:00",
            "out_duration_min": 300,
            "out_stops": 1,
            "out_layovers": [
                {"code": "ORD", "name": "Chicago O'Hare (ORD)", "minutes": 45, "overnight": False},
            ],
            "in_airline": "American Airlines",
            "in_airline_code": "AA",
            "in_depart_at": "2026-06-08T22:30:00",
            "in_arrive_at": "2026-06-09T05:30:00",
            "in_duration_min": 420,
            "in_stops": 0,
            "in_layovers": [],
            "_out_via_codes": ["ORD"],
            "_in_via_codes": [],
            "_sort_total_duration": 720,
        }]

        decorated = _decorate_flights_for_display(flights, {"origin": "JFK", "destination": "LAX"})
        flight = decorated[0]

        self.assertEqual(flight["segments_ui"][0]["time_chip"], "Early morning")
        self.assertEqual(flight["segments_ui"][1]["time_chip"], "Late night")
        self.assertEqual(flight["segments_ui"][0]["layovers"][0]["quality_label"], "Quick connection")
        self.assertEqual(flight["segments_ui"][0]["layovers"][0]["line_position_pct"], 50.0)
        self.assertEqual(flight["segments_ui"][1]["stops_label"], "Nonstop")
        self.assertEqual(flight["segments_ui"][0]["route_chip"], "Via ORD")

    def test_parse_offer_keeps_marketing_airline_and_explains_operating_airline(self):
        offer = {
            "id": "off_codeshare",
            "total_amount": "412.00",
            "total_currency": "USD",
            "passengers": [{"id": "pas_1"}],
            "owner": {
                "name": "American Airlines",
                "iata_code": "AA",
            },
            "slices": [
                {
                    "duration": "PT6H10M",
                    "segments": [
                        {
                            "origin": {"iata_code": "BOS"},
                            "destination": {"iata_code": "DUB"},
                            "departing_at": "2099-07-08T05:25:00",
                            "arriving_at": "2099-07-08T17:24:00",
                            "marketing_carrier": {
                                "name": "American Airlines",
                                "iata_code": "AA",
                            },
                            "operating_carrier": {
                                "name": "British Airways",
                                "iata_code": "BA",
                            },
                            "marketing_carrier_flight_number": "6143",
                        }
                    ],
                }
            ],
        }

        parsed = _parse_offer(offer, {"trip_type": "oneway"}, detailed=True)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["airline_summary"], "American Airlines")
        self.assertEqual(parsed["airline_mix_label"], "Operated by British Airways")

        decorated = _decorate_flights_for_display([parsed], {"trip_type": "oneway"})
        self.assertEqual(decorated[0]["segments_ui"][0]["airline"], "American Airlines")
        self.assertEqual(decorated[0]["segments_ui"][0]["airline_note"], "Operated by British Airways")


if __name__ == "__main__":
    unittest.main()
