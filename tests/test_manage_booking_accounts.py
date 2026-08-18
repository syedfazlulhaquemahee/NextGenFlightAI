import os
import tempfile
import unittest
from unittest.mock import patch

import app as flight_app


class ManageBookingAccountTests(unittest.TestCase):
    def setUp(self):
        flight_app.app.config["TESTING"] = True
        self.client = flight_app.app.test_client()
        self._orig_duffel_token = flight_app.DUFFEL_ACCESS_TOKEN
        self._orig_account_db_path = flight_app.ACCOUNT_DB_PATH
        self._orig_account_db_ready = flight_app._ACCOUNT_DB_READY
        self._tmpdir = tempfile.TemporaryDirectory()
        flight_app.DUFFEL_ACCESS_TOKEN = "duffel_test_mock"
        flight_app.ACCOUNT_DB_PATH = os.path.join(self._tmpdir.name, "accounts-test.db")
        flight_app._ACCOUNT_DB_READY = False
        flight_app.USER_ACCOUNT_CACHE.clear()
        flight_app.MANAGE_BOOKING_ATTEMPT_CACHE.clear()

    def tearDown(self):
        flight_app.DUFFEL_ACCESS_TOKEN = self._orig_duffel_token
        flight_app.ACCOUNT_DB_PATH = self._orig_account_db_path
        flight_app._ACCOUNT_DB_READY = self._orig_account_db_ready
        flight_app.USER_ACCOUNT_CACHE.clear()
        flight_app.MANAGE_BOOKING_ATTEMPT_CACHE.clear()
        self._tmpdir.cleanup()

    @staticmethod
    def _matching_order() -> dict:
        return {
            "id": "ord_abc123",
            "booking_reference": "ABC123",
            "payment_status": "paid",
            "total_amount": "321.45",
            "total_currency": "USD",
            "owner": {"name": "Delta Air Lines"},
            "passengers": [
                {
                    "given_name": "Amelia",
                    "family_name": "Earhart",
                    "born_on": "1987-07-24",
                }
            ],
            "slices": [
                {
                    "duration": "PT6H10M",
                    "segments": [
                        {
                            "origin": {"iata_code": "JFK"},
                            "destination": {"iata_code": "LAX"},
                            "departing_at": "2099-06-01T08:00:00Z",
                            "arriving_at": "2099-06-01T14:10:00Z",
                            "marketing_carrier": {"iata_code": "DL", "name": "Delta Air Lines"},
                            "operating_carrier": {"iata_code": "DL", "name": "Delta Air Lines"},
                        }
                    ],
                }
            ],
        }

    @staticmethod
    def _signup_payload(booking_reference: str = "") -> dict:
        return {
            "first_name": "Amelia",
            "last_name": "Earhart",
            "dob": "1987-07-24",
            "account_email": "traveler@example.com",
            "account_password": "strong-password-123",
            "account_password_confirm": "strong-password-123",
            "accept_terms": "on",
            "booking_reference": booking_reference,
        }

    @patch.object(flight_app.email_service, "send_welcome_email", return_value=(True, "sent"))
    def test_signup_persists_account_and_renders_signed_in_state(self, mock_send_welcome):
        response = self.client.post(
            "/manage-booking/account/signup",
            data=self._signup_payload("ABC123"),
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        # Signup now logs the user straight in (bug fix: it used to bounce
        # them back to the login form even though the session was already
        # set) — the account menu in the header is the real signed-in marker.
        self.assertIn(b'id="portalMenuDropdown"', response.data)
        self.assertTrue(os.path.exists(flight_app.ACCOUNT_DB_PATH))

        flight_app.USER_ACCOUNT_CACHE.clear()
        account = flight_app._account_lookup("traveler@example.com")
        self.assertIsNotNone(account)
        self.assertEqual(account.get("email"), "traveler@example.com")
        self.assertEqual(account.get("first_name"), "Amelia")
        self.assertEqual(account.get("last_name"), "Earhart")
        self.assertIn("ABC123", account.get("linked_booking_references", []))
        mock_send_welcome.assert_called_once()

    @patch.object(flight_app.email_service, "send_welcome_email", return_value=(True, "sent"))
    def test_signup_auto_links_existing_booking_history_by_email(self, mock_send_welcome):
        flight_app._capture_booking_email_links(
            order=self._matching_order(),
            passengers_payload=[{"email": "traveler@example.com"}],
        )

        response = self.client.post(
            "/manage-booking/account/signup",
            data=self._signup_payload(""),
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"linked automatically", response.data)
        flight_app.USER_ACCOUNT_CACHE.clear()
        account = flight_app._account_lookup("traveler@example.com")
        self.assertIsNotNone(account)
        self.assertIn("ABC123", account.get("linked_booking_references", []))
        mock_send_welcome.assert_called_once()

    @patch.object(flight_app.DUFF, "list_orders")
    @patch.object(flight_app.email_service, "send_welcome_email", return_value=(True, "sent"))
    def test_signup_discovers_recent_orders_by_email_when_no_local_link_exists(self, mock_send_welcome, mock_list_orders):
        order = self._matching_order()
        order["passengers"][0]["email"] = "traveler@example.com"
        mock_list_orders.return_value = [order]
        previous_testing = bool(flight_app.app.config.get("TESTING"))
        flight_app.app.config["TESTING"] = False
        # CSRF validation is normally short-circuited by TESTING=True; with it
        # forced off (to exercise the real DUFF.list_orders discovery path),
        # the request needs a real token that matches the session's.
        csrf_token = "test-csrf-token"
        with self.client.session_transaction() as session_state:
            session_state[flight_app._B2C_CSRF_SESSION_KEY] = csrf_token
        payload = self._signup_payload("")
        payload["_csrf"] = csrf_token
        try:
            response = self.client.post(
                "/manage-booking/account/signup",
                data=payload,
                follow_redirects=True,
            )
        finally:
            flight_app.app.config["TESTING"] = previous_testing

        self.assertEqual(response.status_code, 200)
        mock_list_orders.assert_called()
        flight_app.USER_ACCOUNT_CACHE.clear()
        account = flight_app._account_lookup("traveler@example.com")
        self.assertIsNotNone(account)
        self.assertIn("ABC123", account.get("linked_booking_references", []))
        mock_send_welcome.assert_called_once()

    @patch.object(flight_app.DUFF, "list_orders")
    def test_verified_manage_booking_links_reference_to_logged_in_account(self, mock_list_orders):
        mock_list_orders.return_value = [self._matching_order()]

        self.client.post(
            "/manage-booking/account/signup",
            data=self._signup_payload(""),
            follow_redirects=True,
        )

        response = self.client.post(
            "/manage-booking",
            data={
                "booking_reference": "ABC123",
                "last_name": "Earhart",
                "dob": "1987-07-24",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        # A successful lookup renders booking_detail.html; there's no literal
        # "Booking found" string anywhere in the app — the page itself is the signal.
        self.assertIn(b'class="booking-detail-page"', response.data)
        flight_app.USER_ACCOUNT_CACHE.clear()
        account = flight_app._account_lookup("traveler@example.com")
        self.assertIsNotNone(account)
        self.assertIn("ABC123", account.get("linked_booking_references", []))

    @patch.object(flight_app.DUFF, "list_orders")
    def test_open_linked_booking_without_reentering_dob(self, mock_list_orders):
        mock_list_orders.return_value = [self._matching_order()]

        self.client.post(
            "/manage-booking/account/signup",
            data=self._signup_payload("ABC123"),
            follow_redirects=True,
        )

        response = self.client.post("/manage-booking/linked/ABC123", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'class="booking-detail-page"', response.data)
        self.assertIn(b"ABC123", response.data)

    def test_login_rejects_wrong_password(self):
        self.client.post(
            "/manage-booking/account/signup",
            data=self._signup_payload(""),
            follow_redirects=True,
        )
        self.client.post("/manage-booking/account/logout", follow_redirects=True)

        response = self.client.post(
            "/manage-booking/account/login",
            data={
                "account_email": "traveler@example.com",
                "account_password": "wrong-password",
                "booking_reference": "",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid email or password.", response.data)
        self.assertNotIn(b'id="portalMenuDropdown"', response.data)

    def test_signup_requires_password_confirmation_match(self):
        payload = self._signup_payload("")
        payload["account_password_confirm"] = "different-password-456"
        response = self.client.post(
            "/manage-booking/account/signup",
            data=payload,
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Password confirmation does not match.", response.data)
        self.assertNotIn(b"Signed in as", response.data)

    @patch.object(flight_app.email_service, "send_password_reset_code_email", return_value=(True, "sent"))
    def test_reset_password_updates_login_credentials(self, mock_send_reset):
        self.client.post(
            "/manage-booking/account/signup",
            data=self._signup_payload(""),
            follow_redirects=True,
        )
        self.client.post("/manage-booking/account/logout", follow_redirects=True)

        request_response = self.client.post(
            "/manage-booking/account/reset/request",
            data={
                "account_email": "traveler@example.com",
                "booking_reference": "",
            },
            follow_redirects=True,
        )
        self.assertEqual(request_response.status_code, 200)
        self.assertIn(b"Verification code sent", request_response.data)
        mock_send_reset.assert_called_once()

        with self.client.session_transaction() as session_state:
            verification_code = str(session_state.get("ngf_reset_code") or "")
            self.assertTrue(verification_code)

        verify_response = self.client.post(
            "/manage-booking/account/reset/verify",
            data={
                "account_email": "traveler@example.com",
                "verification_code": verification_code,
                "booking_reference": "",
            },
            follow_redirects=True,
        )
        self.assertEqual(verify_response.status_code, 200)
        self.assertIn(b"Verification complete.", verify_response.data)

        with self.client.session_transaction() as session_state:
            reset_token = str(session_state.get("ngf_reset_token") or "")
            self.assertTrue(reset_token)

        reset_response = self.client.post(
            "/manage-booking/account/reset-password",
            data={
                "account_email": "traveler@example.com",
                "reset_token": reset_token,
                "new_password": "new-pass-987",
                "confirm_new_password": "new-pass-987",
                "booking_reference": "",
            },
            follow_redirects=True,
        )
        self.assertEqual(reset_response.status_code, 200)
        self.assertIn(b"Password updated. Please sign in with your new password.", reset_response.data)

        old_login = self.client.post(
            "/manage-booking/account/login",
            data={
                "account_email": "traveler@example.com",
                "account_password": "strong-password-123",
                "booking_reference": "",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Invalid email or password.", old_login.data)

        new_login = self.client.post(
            "/manage-booking/account/login",
            data={
                "account_email": "traveler@example.com",
                "account_password": "new-pass-987",
                "booking_reference": "",
            },
            follow_redirects=True,
        )
        self.assertIn(b'id="portalMenuDropdown"', new_login.data)

    def test_reset_password_rejects_missing_or_invalid_token(self):
        self.client.post(
            "/manage-booking/account/signup",
            data=self._signup_payload(""),
            follow_redirects=True,
        )
        self.client.post("/manage-booking/account/logout", follow_redirects=True)

        response = self.client.post(
            "/manage-booking/account/reset-password",
            data={
                "account_email": "traveler@example.com",
                "reset_token": "",
                "new_password": "new-pass-987",
                "confirm_new_password": "new-pass-987",
                "booking_reference": "",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Reset link is invalid or expired.", response.data)

    @patch.object(flight_app.DUFF, "list_orders")
    def test_open_unlinked_booking_is_denied(self, mock_list_orders):
        mock_list_orders.return_value = [self._matching_order()]
        self.client.post(
            "/manage-booking/account/signup",
            data=self._signup_payload("ABC123"),
            follow_redirects=True,
        )

        response = self.client.post("/manage-booking/linked/ZZZ999", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"That booking is not linked to your account.", response.data)

    @patch.object(flight_app.DUFF, "list_orders")
    def test_manage_booking_rate_limit_locks_after_retries(self, mock_list_orders):
        mismatch = self._matching_order()
        mismatch["passengers"][0]["family_name"] = "Different"
        mock_list_orders.return_value = [mismatch]

        status_codes = []
        for _ in range(9):
            response = self.client.post(
                "/manage-booking",
                data={
                    "booking_reference": "ABC123",
                    "last_name": "Earhart",
                    "dob": "1987-07-24",
                },
                follow_redirects=False,
            )
            status_codes.append(response.status_code)

        self.assertTrue(all(code == 404 for code in status_codes[:8]))
        self.assertEqual(status_codes[-1], 429)

    def test_signup_requires_profile_fields_and_terms(self):
        payload = self._signup_payload("")
        payload["first_name"] = ""
        response = self.client.post(
            "/manage-booking/account/signup",
            data=payload,
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Enter your first name.", response.data)

        payload = self._signup_payload("")
        payload.pop("accept_terms", None)
        response = self.client.post(
            "/manage-booking/account/signup",
            data=payload,
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Accept the Terms and Conditions", response.data)

    @patch.object(flight_app, "search_flights")
    @patch.object(flight_app, "parse_ai_flight_request")
    def test_portal_shows_saved_manual_and_ai_searches(self, mock_parse_ai, mock_search_flights):
        mock_search_flights.return_value = []
        mock_parse_ai.return_value = {
            "origin": "JFK",
            "destination": "LAX",
            "trip_type": "oneway",
            "depart_date": "2099-08-20",
            "return_date": None,
            "passengers": "1",
            "cabin": "ECONOMY",
            "nonstop": False,
            "sort": "recommended",
            "combination_mode": "auto",
            "search_mode": "standard",
        }

        self.client.post(
            "/manage-booking/account/signup",
            data=self._signup_payload(""),
            follow_redirects=True,
        )

        self.client.post(
            "/search",
            data={
                "mode": "standard",
                "origin": "JFK",
                "destination": "LAX",
                "trip_type": "roundtrip",
                "depart_date": "2099-07-10",
                "return_date": "2099-07-17",
                "passengers": "1",
                "cabin": "ECONOMY",
                "sort": "recommended",
                "combination_mode": "auto",
            },
            follow_redirects=True,
        )
        self.client.post(
            "/search",
            data={
                "mode": "ai",
                "ai_text": "One way from JFK to LAX on 2099-08-20",
            },
            follow_redirects=True,
        )

        portal = self.client.get("/portal", follow_redirects=True)
        self.assertEqual(portal.status_code, 200)
        self.assertIn(b"Saved searches", portal.data)
        self.assertIn(b"JFK", portal.data)
        self.assertIn(b"One way from JFK to LAX on 2099-08-20", portal.data)


if __name__ == "__main__":
    unittest.main()
