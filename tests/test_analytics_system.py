import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import analytics_app as analytics_dashboard
import analytics_store
import app as flight_app


class AnalyticsSystemTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._analytics_db_path = os.path.join(self._tmpdir.name, "analytics-test.db")
        self._accounts_db_path = os.path.join(self._tmpdir.name, "accounts-test.db")
        self._orig_analytics_db_path = analytics_store.ANALYTICS_DB_PATH
        self._orig_ip_salt = analytics_store.ANALYTICS_IP_SALT
        self._orig_account_db_path = flight_app.ACCOUNT_DB_PATH
        self._orig_account_db_ready = flight_app._ACCOUNT_DB_READY
        self._orig_dashboard_accounts_db_path = analytics_dashboard.ACCOUNTS_DB_PATH
        analytics_store.configure(db_path=self._analytics_db_path, ip_salt="test-salt")
        analytics_store._ANALYTICS_DB_READY = False
        analytics_store.clear_events()

        flight_app.app.config["TESTING"] = True
        flight_app.app.config["NGF_ENABLE_ANALYTICS_IN_TESTS"] = True
        flight_app.ACCOUNT_DB_PATH = self._accounts_db_path
        flight_app._ACCOUNT_DB_READY = False
        flight_app.USER_ACCOUNT_CACHE.clear()
        self.client = flight_app.app.test_client()

        analytics_dashboard.analytics_store.configure(db_path=self._analytics_db_path, ip_salt="test-salt")
        analytics_dashboard.analytics_store._ANALYTICS_DB_READY = False
        analytics_dashboard.ACCOUNTS_DB_PATH = self._accounts_db_path
        analytics_dashboard.app.config["TESTING"] = True
        self.analytics_client = analytics_dashboard.app.test_client()

    def tearDown(self):
        analytics_store.configure(db_path=self._orig_analytics_db_path, ip_salt=self._orig_ip_salt)
        analytics_store._ANALYTICS_DB_READY = False
        flight_app.ACCOUNT_DB_PATH = self._orig_account_db_path
        flight_app._ACCOUNT_DB_READY = self._orig_account_db_ready
        flight_app.USER_ACCOUNT_CACHE.clear()
        analytics_dashboard.ACCOUNTS_DB_PATH = self._orig_dashboard_accounts_db_path
        self._tmpdir.cleanup()

    @staticmethod
    def _future_date(days: int) -> str:
        return (date.today() + timedelta(days=days)).isoformat()

    def test_standard_search_writes_search_completed_event(self):
        with patch("app.search_flights", return_value=[]):
            response = self.client.post(
                "/search",
                data={
                    "mode": "standard",
                    "search_submitted": "1",
                    "origin": "JFK",
                    "destination": "LAX",
                    "trip_type": "oneway",
                    "depart_date": self._future_date(14),
                    "passengers": "1",
                    "cabin": "ECONOMY",
                    "sort": "recommended",
                },
            )

        self.assertEqual(response.status_code, 200)
        events = analytics_store.fetch_recent_events(limit=20)
        search_events = [event for event in events if event.get("event_type") == "search_completed"]
        self.assertTrue(search_events)
        latest = search_events[0]
        self.assertEqual(latest.get("search_mode"), "standard")
        self.assertEqual(latest.get("origin"), "JFK")
        self.assertEqual(latest.get("destination"), "LAX")
        self.assertFalse(latest.get("success"))

    def test_update_results_writes_separate_results_updated_event(self):
        with patch("app.search_flights", return_value=[]):
            first = self.client.post(
                "/search",
                data={
                    "mode": "standard",
                    "search_submitted": "1",
                    "origin": "JFK",
                    "destination": "LAX",
                    "trip_type": "oneway",
                    "depart_date": self._future_date(14),
                    "passengers": "1",
                    "cabin": "ECONOMY",
                    "sort": "recommended",
                },
            )
            self.assertEqual(first.status_code, 200)

            second = self.client.post(
                "/search",
                data={
                    "mode": "standard",
                    "search_submitted": "1",
                    "force_refresh": "1",
                    "origin": "JFK",
                    "destination": "LAX",
                    "trip_type": "oneway",
                    "depart_date": self._future_date(14),
                    "passengers": "2",
                    "cabin": "BUSINESS",
                    "sort": "cheapest",
                },
            )
            self.assertEqual(second.status_code, 200)

        events = analytics_store.fetch_recent_events(limit=20)
        update_events = [event for event in events if event.get("event_type") == "results_updated"]
        self.assertTrue(update_events)
        latest = update_events[0]
        changed_fields = latest.get("metadata", {}).get("changed_fields") or []
        self.assertIn("passengers", changed_fields)
        self.assertIn("cabin", changed_fields)
        self.assertIn("sort", changed_fields)

    def test_signup_writes_account_signup_event(self):
        email = f"avi-{os.path.basename(self._tmpdir.name)}@example.com"
        response = self.client.post(
            "/manage-booking/account/signup",
            data={
                "first_name": "Avi",
                "last_name": "Traveler",
                "dob": "1990-06-10",
                "account_email": email,
                "account_password": "pass12345",
                "account_password_confirm": "pass12345",
                "accept_terms": "on",
                "booking_reference": "",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        events = analytics_store.fetch_recent_events(limit=20)
        signup_events = [event for event in events if event.get("event_type") == "account_signup"]
        self.assertTrue(signup_events)
        latest = signup_events[0]
        self.assertEqual(latest.get("account_email"), email)
        self.assertTrue(latest.get("success"))

    def test_dashboard_api_exposes_collected_top_routes(self):
        analytics_store.record_event(
            event_type="search_completed",
            anon_id="anon_test",
            account_email="",
            location_country="US",
            search_mode="standard",
            origin="JFK",
            destination="SFO",
            trip_type="oneway",
            result_count=12,
            success=True,
            metadata={"source": "unit_test"},
        )
        analytics_store.record_event(
            event_type="search_completed",
            anon_id="anon_test",
            account_email="",
            location_country="US",
            search_mode="standard",
            origin="JFK",
            destination="SFO",
            trip_type="oneway",
            result_count=10,
            success=True,
            metadata={"source": "unit_test"},
        )

        response = self.analytics_client.get("/api/overview?days=30")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIsInstance(payload, dict)
        routes = payload.get("top_routes") or []
        self.assertTrue(routes)
        self.assertEqual(routes[0].get("route"), "JFK -> SFO")
        self.assertEqual(routes[0].get("searches"), 2)
        self.assertIn("results_updates", payload)
        self.assertIn("results_clicks", payload)
        self.assertIn("next_pages", payload)

    def test_funnel_summary_counts_search_click_and_intent(self):
        analytics_store.record_event(event_type="site_landed", anon_id="anon_1", search_id="s1", success=True)
        analytics_store.record_event(
            event_type="search_completed",
            anon_id="anon_1",
            search_id="s1",
            origin="JFK",
            destination="LAX",
            success=True,
            metadata={"nonstop": True},
        )
        analytics_store.record_event(
            event_type="results_viewed",
            anon_id="anon_1",
            search_id="s1",
            origin="JFK",
            destination="LAX",
            success=True,
        )
        analytics_store.record_event(
            event_type="flight_selected",
            anon_id="anon_1",
            search_id="s1",
            origin="JFK",
            destination="LAX",
            success=True,
            metadata={"price": 310, "airline": "Delta", "nonstop": True},
        )
        analytics_store.record_event(
            event_type="booking_intent",
            anon_id="anon_1",
            search_id="s1",
            origin="JFK",
            destination="LAX",
            success=True,
            metadata={"price": 310, "airline": "Delta", "nonstop": True},
        )

        summary = analytics_store.fetch_funnel_summary(days=30)
        self.assertEqual(summary["landed_users"], 1)
        self.assertEqual(summary["searched_users"], 1)
        self.assertEqual(summary["clicked_users"], 1)
        self.assertEqual(summary["intent_users"], 1)
        self.assertEqual(summary["search_rate"], 100.0)
        self.assertEqual(summary["click_rate"], 100.0)
        self.assertEqual(summary["intent_rate"], 100.0)

    def test_clear_behavioral_events_preserves_booking_completed(self):
        analytics_store.record_event(event_type="site_landed", anon_id="anon_1", search_id="s1", success=True)
        analytics_store.record_event(
            event_type="booking_completed",
            anon_id="anon_1",
            search_id="s1",
            origin="JFK",
            destination="LAX",
            success=True,
            booking_amount=499.99,
            currency="USD",
        )

        deleted = analytics_store.clear_behavioral_events()
        self.assertEqual(deleted, 1)

        events = analytics_store.fetch_recent_events(limit=10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "booking_completed")


if __name__ == "__main__":
    unittest.main()
