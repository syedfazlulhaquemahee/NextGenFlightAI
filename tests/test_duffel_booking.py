import unittest
from unittest.mock import patch

import app as flight_app
from duffel_booking import build_checkout_summary


class DuffelBookingFlowTests(unittest.TestCase):
    def setUp(self):
        self._orig_testing = flight_app.app.config.get("TESTING")
        flight_app.app.config["TESTING"] = True
        self.client = flight_app.app.test_client()
        self._orig_duffel_token = flight_app.DUFFEL_ACCESS_TOKEN
        self._orig_duffel_env = flight_app.DUFFEL_ENV
        self._orig_payment_mode = flight_app.DUFFEL_PAYMENT_MODE
        flight_app.DUFFEL_ACCESS_TOKEN = "duffel_test_mock"
        flight_app.DUFFEL_ENV = "test"
        flight_app.RECENT_ORDER_CACHE.clear()

    def tearDown(self):
        flight_app.app.config["TESTING"] = self._orig_testing
        flight_app.DUFFEL_ACCESS_TOKEN = self._orig_duffel_token
        flight_app.DUFFEL_ENV = self._orig_duffel_env
        flight_app.DUFFEL_PAYMENT_MODE = self._orig_payment_mode
        flight_app.RECENT_ORDER_CACHE.clear()

    @staticmethod
    def _offer_two_adults(*, expires_at: str = "2099-06-01T12:00:00Z") -> dict:
        base = DuffelBookingFlowTests._offer(expires_at=expires_at)
        base["passengers"] = [
            {"id": "pas_1", "type": "adult"},
            {"id": "pas_2", "type": "adult"},
        ]
        base["total_amount"] = "642.90"
        return base

    @staticmethod
    def _valid_form_two() -> dict:
        form = DuffelBookingFlowTests._valid_form()
        form.update(
            {
                "traveler_1_title": "mr",
                "traveler_1_given_name": "Charles",
                "traveler_1_family_name": "Lindbergh",
                "traveler_1_born_on": "1988-01-15",
                "traveler_1_gender": "m",
                "traveler_1_email": "charles@example.com",
                "traveler_1_phone_number": "+15559876543",
            }
        )
        return form

    @staticmethod
    def _offer(*, expires_at: str = "2099-06-01T12:00:00Z", requires_identity_documents: bool = False) -> dict:
        return {
            "id": "off_123",
            "expires_at": expires_at,
            "total_amount": "321.45",
            "total_currency": "USD",
            "owner": {"name": "Delta Air Lines"},
            "passengers": [{"id": "pas_1", "type": "adult"}],
            "passenger_identity_documents_required": requires_identity_documents,
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
                            "passengers": [{"cabin_class_marketing_name": "Economy"}],
                        }
                    ],
                }
            ],
        }

    @staticmethod
    def _order_two() -> dict:
        base = DuffelBookingFlowTests._order()
        base["passengers"] = [
            {"given_name": "Amelia", "family_name": "Earhart"},
            {"given_name": "Charles", "family_name": "Lindbergh"},
        ]
        return base

    @staticmethod
    def _order() -> dict:
        return {
            "id": "ord_123",
            "booking_reference": "ABC123",
            "payment_status": "paid",
            "total_amount": "321.45",
            "total_currency": "USD",
            "owner": {"name": "Delta Air Lines"},
            "passengers": [{"given_name": "Amelia", "family_name": "Earhart"}],
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
    def _valid_form() -> dict:
        return {
            "traveler_0_title": "ms",
            "traveler_0_given_name": "Amelia",
            "traveler_0_family_name": "Earhart",
            "traveler_0_born_on": "1987-07-24",
            "traveler_0_gender": "f",
            "traveler_0_email": "amelia@example.com",
            "traveler_0_phone_number": "+15551234567",
        }

    @patch.object(flight_app.DUFF, "get_offer")
    def test_checkout_get_renders_offer_summary(self, mock_get_offer):
        mock_get_offer.return_value = self._offer()

        response = self.client.get("/checkout/off_123")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Review your trip", response.data)
        self.assertIn(b"Delta Air Lines", response.data)
        self.assertIn(b"JFK to LAX", response.data)
        self.assertIn(b"checkout", response.data.lower())
        self.assertIn(b"assets.duffel.com/img/airlines/for-light-background/full-color-logo/DL.svg", response.data)

    @patch.object(flight_app.DUFF, "get_offer")
    def test_checkout_review_lists_two_travelers(self, mock_get_offer):
        mock_get_offer.return_value = self._offer_two_adults()

        response = self.client.get("/checkout/off_123")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Traveler 1", response.data)
        self.assertIn(b"Traveler 2", response.data)
        self.assertIn(b"split evenly", response.data)

    @patch.object(flight_app.DUFF, "get_offer")
    def test_seat_selection_get_renders_page(self, mock_get_offer):
        mock_get_offer.return_value = self._offer()

        response = self.client.get("/checkout/off_123/seats")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Bags, seats", response.data)
        self.assertIn(b"duffel-ancillaries", response.data)
        self.assertIn(b"Next: Traveler checkout", response.data)

    @patch.object(flight_app.DUFF, "get_offer")
    def test_seat_selection_lists_multiple_travelers(self, mock_get_offer):
        mock_get_offer.return_value = self._offer_two_adults()

        response = self.client.get("/checkout/off_123/seats")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Traveler 1", response.data)
        self.assertIn(b"Traveler 2", response.data)
        self.assertIn(b"duffel-ancillaries", response.data)
        self.assertIn(b"duffelAncillariesEmbed", response.data)

    @patch.object(flight_app.DUFF, "get_offer")
    def test_checkout_details_get_renders_form(self, mock_get_offer):
        mock_get_offer.return_value = self._offer()

        response = self.client.get("/checkout/off_123/details")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Traveler and payment details", response.data)
        self.assertIn(b'traveler_0_title', response.data)
        self.assertIn(b"Complete booking", response.data)
        self.assertIn(b"assets.duffel.com/img/airlines/for-light-background/full-color-logo/DL.svg", response.data)

    @patch.object(flight_app.DUFF, "create_order")
    @patch.object(flight_app.DUFF, "get_offer")
    def test_checkout_post_validates_required_fields(self, mock_get_offer, mock_create_order):
        mock_get_offer.return_value = self._offer()

        response = self.client.post("/checkout/off_123/details", data={"traveler_0_given_name": ""})

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Enter a first name.", response.data)
        self.assertIn(b"Enter a last name.", response.data)
        mock_create_order.assert_not_called()

    @patch.object(flight_app, "_send_itinerary_emails_after_booking")
    @patch.object(flight_app.DUFF, "create_order")
    @patch.object(flight_app.DUFF, "get_offer")
    def test_checkout_post_creates_order_and_redirects_to_confirmation(self, mock_get_offer, mock_create_order, mock_send_itinerary):
        mock_get_offer.return_value = self._offer()
        mock_create_order.return_value = self._order()

        response = self.client.post("/checkout/off_123/details", data=self._valid_form(), follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ABC123", response.data)
        self.assertIn(b"ord_123", response.data)
        self.assertIn(b"Amelia Earhart", response.data)
        mock_create_order.assert_called_once()
        kwargs = mock_create_order.call_args.kwargs
        self.assertEqual(kwargs["offer_id"], "off_123")
        self.assertEqual(kwargs["total_amount"], "321.45")
        self.assertEqual(kwargs["total_currency"], "USD")
        self.assertEqual(kwargs["passengers"][0]["id"], "pas_1")
        self.assertEqual(kwargs["passengers"][0]["title"], "ms")
        self.assertEqual(kwargs["passengers"][0]["given_name"], "Amelia")
        mock_send_itinerary.assert_called_once()

    @patch.object(flight_app, "_send_itinerary_emails_after_booking")
    @patch.object(flight_app.DUFF, "create_order")
    @patch.object(flight_app.DUFF, "create_component_client_key")
    @patch.object(flight_app.DUFF, "get_seat_maps")
    @patch.object(flight_app.DUFF, "get_offer")
    def test_checkout_post_card_mode_sends_three_d_secure_payment(
        self,
        mock_get_offer,
        mock_get_seat_maps,
        mock_create_component_client_key,
        mock_create_order,
        mock_send_itinerary,
    ):
        flight_app.app.config["TESTING"] = False
        flight_app.DUFFEL_PAYMENT_MODE = "card"
        mock_get_offer.return_value = self._offer()
        mock_get_seat_maps.return_value = []
        mock_create_component_client_key.return_value = "cck_test_123"
        mock_create_order.return_value = self._order()

        with self.client as client:
            get_response = client.get("/checkout/off_123/details")
            self.assertEqual(get_response.status_code, 200)
            self.assertIn(b"duffel-card-form", get_response.data)
            with client.session_transaction() as sess:
                csrf = sess[flight_app._B2C_CSRF_SESSION_KEY]

            form = self._valid_form()
            form["_csrf"] = csrf
            form["duffel_three_d_secure_session_id"] = "tds_test_123"
            response = client.post("/checkout/off_123/details", data=form, follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ABC123", response.data)
        mock_create_order.assert_called_once()
        kwargs = mock_create_order.call_args.kwargs
        self.assertEqual(
            kwargs["payments"],
            [
                {
                    "type": "card",
                    "currency": "USD",
                    "amount": "321.45",
                    "three_d_secure_session_id": "tds_test_123",
                }
            ],
        )
        mock_send_itinerary.assert_called_once()

    @patch.object(flight_app, "_send_itinerary_emails_after_booking")
    @patch.object(flight_app.DUFF, "create_order")
    @patch.object(flight_app.DUFF, "get_offer")
    def test_checkout_post_two_passengers(self, mock_get_offer, mock_create_order, mock_send_itinerary):
        mock_get_offer.return_value = self._offer_two_adults()
        mock_create_order.return_value = self._order_two()

        response = self.client.post("/checkout/off_123/details", data=self._valid_form_two(), follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ABC123", response.data)
        mock_create_order.assert_called_once()
        kwargs = mock_create_order.call_args.kwargs
        self.assertEqual(len(kwargs["passengers"]), 2)
        self.assertEqual(kwargs["passengers"][0]["id"], "pas_1")
        self.assertEqual(kwargs["passengers"][1]["id"], "pas_2")
        self.assertEqual(kwargs["passengers"][1]["given_name"], "Charles")
        mock_send_itinerary.assert_called_once()

    @patch.object(flight_app.DUFF, "get_offer")
    def test_checkout_blocks_expired_offer(self, mock_get_offer):
        mock_get_offer.return_value = self._offer(expires_at="2000-01-01T00:00:00Z")

        response = self.client.get("/checkout/off_123")

        self.assertEqual(response.status_code, 410)
        self.assertIn(b"This offer has expired", response.data)

    def test_checkout_summary_adds_seat_prices_from_catalog_when_payload_has_ids_only(self):
        offer = self._offer()
        offer["available_services"] = [
            {
                "id": "aseat_1",
                "total_amount": "45.00",
                "total_currency": "USD",
                "type": "seat",
                "passenger_id": "pas_1",
                "segment_id": "seg_1",
            }
        ]
        payload = {"selected_services": [{"id": "aseat_1", "quantity": 1}]}
        summary = build_checkout_summary(offer, seat_maps=[], ancillaries_payload=payload)
        self.assertEqual(summary["price_breakdown"]["ancillaries_amount"], "45.00")
        self.assertEqual(summary["price_breakdown"]["total_amount"], "366.45")

    def test_checkout_summary_joins_route_and_city_summaries_for_multicity(self):
        offer = self._offer()
        offer["slices"] = [
            {
                "duration": "PT7H00M",
                "segments": [
                    {
                        "origin": {"iata_code": "JFK", "city_name": "New York"},
                        "destination": {"iata_code": "LHR", "city_name": "London"},
                        "departing_at": "2099-06-01T08:00:00Z",
                        "arriving_at": "2099-06-01T15:00:00Z",
                        "marketing_carrier": {"iata_code": "DL", "name": "Delta Air Lines"},
                        "operating_carrier": {"iata_code": "DL", "name": "Delta Air Lines"},
                    }
                ],
            },
            {
                "duration": "PT1H20M",
                "segments": [
                    {
                        "origin": {"iata_code": "LHR", "city_name": "London"},
                        "destination": {"iata_code": "CDG", "city_name": "Paris"},
                        "departing_at": "2099-06-04T09:00:00Z",
                        "arriving_at": "2099-06-04T10:20:00Z",
                        "marketing_carrier": {"iata_code": "AF", "name": "Air France"},
                        "operating_carrier": {"iata_code": "AF", "name": "Air France"},
                    }
                ],
            },
            {
                "duration": "PT2H10M",
                "segments": [
                    {
                        "origin": {"iata_code": "CDG", "city_name": "Paris"},
                        "destination": {"iata_code": "FCO", "city_name": "Rome"},
                        "departing_at": "2099-06-08T12:00:00Z",
                        "arriving_at": "2099-06-08T14:10:00Z",
                        "marketing_carrier": {"iata_code": "AZ", "name": "ITA Airways"},
                        "operating_carrier": {"iata_code": "AZ", "name": "ITA Airways"},
                    }
                ],
            },
        ]

        summary = build_checkout_summary(offer, seat_maps=[], ancillaries_payload={})

        self.assertEqual(summary["route_summary"], "JFK \u2192 LHR")
        self.assertEqual(summary["city_summary"], "New York to London")

    @patch.object(flight_app.DUFF, "get_order")
    def test_confirmation_fetches_order_when_cache_is_empty(self, mock_get_order):
        mock_get_order.return_value = self._order()
        with self.client.session_transaction() as sess:
            sess[flight_app._SESSION_AUTHORIZED_ORDERS_KEY] = ["ord_123"]

        response = self.client.get("/booking/confirmation/ord_123")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ABC123", response.data)
        self.assertIn(b"ABC123", response.data)
        mock_get_order.assert_called_once_with("ord_123")


if __name__ == "__main__":
    unittest.main()
