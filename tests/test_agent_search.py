import os
import tempfile
import unittest
from unittest.mock import patch

import agent_store
import app as flight_app


class AgentSearchTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_agent_db_path = agent_store.AGENT_DB_PATH
        self._orig_agent_db_ready = agent_store._AGENT_DB_READY
        self._orig_bootstrap = {
            key: os.environ.get(key)
            for key in (
                "NGF_AGENT_BOOTSTRAP_EMAIL",
                "NGF_AGENT_BOOTSTRAP_PASSWORD",
                "NGF_AGENT_BOOTSTRAP_AGENCY",
                "NGF_AGENT_BOOTSTRAP_FIRST_NAME",
                "NGF_AGENT_BOOTSTRAP_LAST_NAME",
                "NGF_AGENT_BOOTSTRAP_ROLE",
            )
        }
        for key in self._orig_bootstrap:
            os.environ.pop(key, None)
        flight_app.app.config["TESTING"] = True
        agent_store.configure(db_path=os.path.join(self._tmpdir.name, "agent-search-test.db"))
        self.client = flight_app.app.test_client()

        agency = agent_store.create_agency("Orbit Partners", code="orbit-partners")
        self.user = agent_store.create_user(
            email="agent-search@example.com",
            password="agent-pass-123",
            first_name="Ava",
            last_name="Agent",
            global_role="super_admin",
            agency_id=int(agency.get("id") or 0),
            membership_role="super_admin",
            two_factor_enabled=True,
            totp_secret="JBSWY3DPEHPK3PXP",
        )
        with self.client.session_transaction() as session_state:
            session_state["ngf_agent_user_id"] = int(self.user.get("id") or 0)
            session_state["ngf_agent_session_version"] = int(self.user.get("session_version") or 1)
            session_state["ngf_agent_last_seen_at"] = "2099-01-01T00:00:00+00:00"
            session_state["ngf_agent_csrf"] = "test-csrf"

    def tearDown(self):
        agent_store.AGENT_DB_PATH = self._orig_agent_db_path
        agent_store._AGENT_DB_READY = self._orig_agent_db_ready
        for key, value in self._orig_bootstrap.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmpdir.cleanup()

    def test_agent_search_page_loads_for_signed_in_user(self):
        response = self.client.get("/agent/search")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Book Flights", response.data)

    def test_agent_operational_routes_render_real_sections(self):
        for path, marker in (
            ("/agent/bookings", b"Booking ledger"),
            ("/agent/finance", b"Finance"),
            ("/agent/logs", b"Auth events"),
            ("/agent/users", b"Users"),
            ("/agent/agencies", b"Agencies"),
            ("/agent/requests", b"User add requests"),
            ("/agent/settings/markup", b"Markup configuration"),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(marker, response.data, path)

    @patch("app.offer_has_expired", return_value=False)
    @patch("app.build_checkout_page_model")
    @patch("app.build_checkout_summary")
    @patch("app.build_traveler_forms")
    @patch("app.DUFF.get_offer")
    def test_agent_checkout_page_loads(
        self,
        get_offer_mock,
        travelers_mock,
        summary_mock,
        page_model_mock,
        expired_mock,
    ):
        get_offer_mock.return_value = {"id": "off_123", "owner": {"name": "Delta Air Lines"}}
        travelers_mock.return_value = [{"index": 0, "label": "Traveler 1", "type_label": "Adult", "expanded": True}]
        summary_mock.return_value = {
            "airline_name": "Delta Air Lines",
            "total_amount": "512.40",
            "currency": "USD",
            "cabin_label": "Economy",
            "slices": [],
            "flexibility": {},
        }
        page_model_mock.return_value = {
            "payment": {
                "title": "Duffel balance",
                "supporting_copy": "Test mode payment.",
                "mode": "balance",
                "component_client_key_available": False,
            }
        }

        response = self.client.get("/agent/checkout/off_123")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Review & Book", response.data)
        self.assertIn(b"Passenger details", response.data)

    @patch("app._send_itinerary_emails_after_booking")
    @patch("app._track_booking_completed_event")
    @patch("app._capture_booking_email_links")
    @patch("app.offer_has_expired", return_value=False)
    @patch("app.validate_checkout_form")
    @patch("app.build_checkout_page_model")
    @patch("app.build_checkout_summary")
    @patch("app.build_traveler_forms")
    @patch("app.DUFF.create_order")
    @patch("app.DUFF.get_offer")
    def test_agent_checkout_post_creates_booking_and_redirects(
        self,
        get_offer_mock,
        create_order_mock,
        travelers_mock,
        summary_mock,
        page_model_mock,
        validate_mock,
        expired_mock,
        capture_mock,
        track_mock,
        email_mock,
    ):
        offer = {
            "id": "off_123",
            "owner": {"name": "Delta Air Lines"},
            "slices": [],
        }
        get_offer_mock.return_value = offer
        travelers = [{"index": 0, "label": "Traveler 1", "type_label": "Adult", "expanded": True}]
        travelers_mock.return_value = travelers
        summary_mock.return_value = {
            "airline_name": "Delta Air Lines",
            "total_amount": "512.40",
            "currency": "USD",
            "cabin_label": "Economy",
            "slices": [],
            "flexibility": {},
        }
        page_model_mock.return_value = {
            "payment": {
                "title": "Duffel balance",
                "supporting_copy": "Test mode payment.",
                "mode": "balance",
                "component_client_key_available": False,
            }
        }
        passengers_payload = [
            {
                "type": "adult",
                "title": "mr",
                "given_name": "Ava",
                "family_name": "Agent",
                "born_on": "1990-01-01",
                "gender": "m",
                "email": "ava@example.com",
                "phone_number": "+15551234567",
            }
        ]
        validate_mock.return_value = (passengers_payload, travelers, {})
        create_order_mock.return_value = {
            "id": "ord_123",
            "booking_reference": "ABC123",
            "total_amount": "512.40",
            "total_currency": "USD",
            "passengers": [
                {"given_name": "Ava", "family_name": "Agent", "email": "ava@example.com"}
            ],
            "slices": [],
        }

        response = self.client.post("/agent/checkout/off_123", data={"_csrf": "test-csrf"}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/agent/booking/confirmation/ord_123", response.headers.get("Location", ""))

        booking = agent_store.get_platform_booking_by_order_id("ord_123")
        self.assertTrue(booking)
        self.assertEqual(booking.get("duffel_offer_id"), "off_123")

    @patch("app.search_flights")
    def test_agent_search_reuses_shared_search_backend(self, search_mock):
        search_mock.return_value = [
            {
                "offer_id": "off_123",
                "price": 321.45,
                "currency": "USD",
                "airline_summary": "Delta Air Lines",
                "passenger_count": 1,
                "total_duration_min": 370,
                "smart_badge": "Best",
                "metric_is_cheapest": True,
                "metric_is_fastest": False,
                "segments_ui": [
                    {
                        "label": "Outbound",
                        "duration": "6h 10m",
                        "stops_label": "Nonstop",
                        "route_chip": "",
                        "airline": "Delta Air Lines",
                        "origin": "JFK",
                        "destination": "LAX",
                        "depart_time": "8:00 AM",
                        "depart_day": "Mon, Jun 1",
                        "arrive_time": "11:10 AM",
                        "arrive_day": "Mon, Jun 1",
                        "layovers": [],
                    }
                ],
            }
        ]
        response = self.client.post(
            "/agent/search",
            data={
                "_csrf": "test-csrf",
                "origin": "JFK",
                "destination": "LAX",
                "trip_type": "oneway",
                "depart_date": "2099-06-01",
                "passengers": "1",
                "cabin": "ECONOMY",
                "sort": "recommended",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Delta Air Lines", response.data)
        search_mock.assert_called_once()

    @patch("app.search_flights")
    def test_agent_search_renders_layover_details_for_stops(self, search_mock):
        search_mock.return_value = [
            {
                "offer_id": "off_stop_123",
                "price": 412.30,
                "currency": "USD",
                "airline_summary": "United Airlines",
                "passenger_count": 1,
                "total_duration_min": 485,
                "smart_badge": "",
                "metric_is_cheapest": False,
                "metric_is_fastest": False,
                "segments_ui": [
                    {
                        "label": "Outbound",
                        "duration": "8h 05m",
                        "stops_label": "1 Stop",
                        "route_chip": "Via ORD",
                        "narrative": "Morning departure • 1 stop • via ord",
                        "airline": "United Airlines",
                        "origin": "JFK",
                        "destination": "LAX",
                        "depart_time": "8:15 AM",
                        "depart_day": "Mon, Jun 1",
                        "arrive_time": "1:20 PM",
                        "arrive_day": "Mon, Jun 1",
                        "layovers": [
                            {
                                "code": "ORD",
                                "name": "Chicago O'Hare",
                                "duration_label": "1h 25m",
                                "quality_label": "Comfortable connection",
                                "overnight": False,
                            }
                        ],
                    }
                ],
            }
        ]
        response = self.client.post(
            "/agent/search",
            data={
                "_csrf": "test-csrf",
                "origin": "JFK",
                "destination": "LAX",
                "trip_type": "oneway",
                "depart_date": "2099-06-01",
                "passengers": "1",
                "cabin": "ECONOMY",
                "sort": "recommended",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"1 Stop", response.data)
        self.assertIn(b"Chicago O&#39;Hare", response.data)
        self.assertIn(b"Layover details", response.data)
