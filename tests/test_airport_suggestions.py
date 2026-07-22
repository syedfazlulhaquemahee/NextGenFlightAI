import unittest
from unittest.mock import patch

from app import _local_airport_suggest, app


def _codes(suggestions):
    return [row["code"] for row in suggestions]


class AirportSuggestionTests(unittest.TestCase):
    def test_exact_iata_query_returns_exact_airport_first(self):
        suggestions = _local_airport_suggest("jfk", limit=5)

        self.assertTrue(suggestions)
        self.assertEqual(suggestions[0]["code"], "JFK")

    def test_three_letter_city_prefix_prefers_relevant_city_airports(self):
        suggestions = _local_airport_suggest("los", limit=5)

        self.assertTrue(suggestions)
        self.assertEqual(suggestions[0]["code"], "LAX")

    def test_metro_alias_only_lists_that_metro_airports(self):
        suggestions = _local_airport_suggest("nyc", limit=8)
        codes = _codes(suggestions)

        self.assertEqual(set(codes), {"JFK", "LGA", "EWR"})
        self.assertNotIn("LHR", codes)
        self.assertNotIn("LGW", codes)

    def test_city_code_bjs_only_lists_beijing_airports(self):
        suggestions = _local_airport_suggest("bjs", limit=8)
        codes = _codes(suggestions)

        self.assertEqual(set(codes), {"PEK", "PKX"})
        self.assertNotIn("LHR", codes)

    def test_city_name_search_excludes_unrelated_hubs(self):
        suggestions = _local_airport_suggest("chennai", limit=15)
        codes = _codes(suggestions)
        self.assertNotIn("LHR", codes)
        self.assertNotIn("LGW", codes)
        self.assertIn("MAA", codes)

    def test_gibberish_city_query_returns_no_results(self):
        suggestions = _local_airport_suggest("qqqzzz", limit=8)
        self.assertEqual(_codes(suggestions), [])

    def test_local_iata_city_suggestion_beijing_alias(self):
        from app import _local_iata_city_suggestions

        rows = _local_iata_city_suggestions("beijing")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["code"], "BJS")
        self.assertEqual(rows[0].get("subType"), "CITY")

    def test_airports_route_starts_at_three_characters(self):
        with app.test_client() as client:
            resp = client.get("/airports?q=la")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [])

    def test_airports_route_uses_local_results_for_exact_iata(self):
        with patch("app.DUFF.search_places", return_value=[]):
            with app.test_client() as client:
                resp = client.get("/airports?q=jfk")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data)
        self.assertEqual(data[0]["code"], "JFK")

    def test_airports_metro_prefers_duffel_city_before_local_airports(self):
        remote = [
            {"code": "NYC", "label": "NYC — New York (all airports) (US)", "subType": "CITY"},
            {"code": "JFK", "label": "JFK — Kennedy (US)", "subType": "AIRPORT"},
        ]
        with patch("app.DUFF.search_places", return_value=remote):
            with app.test_client() as client:
                resp = client.get("/airports?q=nyc")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data)
        self.assertEqual(data[0]["code"], "NYC")
        self.assertEqual(data[0].get("subType"), "CITY")
        # Local metro airports follow, without unrelated hubs.
        codes = [row["code"] for row in data]
        self.assertIn("JFK", codes)
        self.assertNotIn("LHR", codes)

    def test_airports_short_query_prepends_grouped_city_row(self):
        with patch("app.DUFF.search_places", return_value=[]):
            with app.test_client() as client:
                resp = client.get("/airports?q=rome")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data)
        self.assertEqual(data[0]["code"], "ROM")
        self.assertEqual(data[0].get("subType"), "CITY")


if __name__ == "__main__":
    unittest.main()
