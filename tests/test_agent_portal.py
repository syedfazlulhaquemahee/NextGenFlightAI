import os
import tempfile
import unittest

import agent_store
import app as flight_app
from agent_security import generate_backup_codes, generate_totp_code, generate_totp_secret, hash_backup_code


class AgentPortalTests(unittest.TestCase):
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
        agent_store.configure(db_path=os.path.join(self._tmpdir.name, "agent-test.db"))
        self.client = flight_app.app.test_client()

    def tearDown(self):
        agent_store.AGENT_DB_PATH = self._orig_agent_db_path
        agent_store._AGENT_DB_READY = self._orig_agent_db_ready
        for key, value in self._orig_bootstrap.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmpdir.cleanup()

    def _create_user(
        self,
        *,
        email: str,
        password: str = "agent-pass-123",
        role: str = "agent_user",
        two_factor_enabled: bool = False,
        secret: str = "",
    ) -> dict:
        agency = agent_store.create_agency("Orbit Partners", code="orbit-partners")
        return agent_store.create_user(
            email=email,
            password=password,
            first_name="Ava",
            last_name="Agent",
            global_role=role,
            agency_id=int(agency.get("id") or 0),
            membership_role=role,
            two_factor_enabled=two_factor_enabled,
            totp_secret=secret,
        )

    def _login_agent(self, *, email: str, password: str, secret: str) -> None:
        self.client.post(
            "/agent/login",
            data={"email": email, "password": password},
            follow_redirects=False,
        )
        self.client.post(
            "/agent/verify-2fa",
            data={"verification_code": generate_totp_code(secret)},
            follow_redirects=True,
        )

    def test_consumer_homepage_still_loads(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_agent_login_requires_two_factor_setup_before_dashboard(self):
        self._create_user(email="agent@example.com")

        login_response = self.client.post(
            "/agent/login",
            data={"email": "agent@example.com", "password": "agent-pass-123"},
            follow_redirects=False,
        )

        self.assertEqual(login_response.status_code, 302)
        self.assertIn("/agent/setup-2fa", login_response.headers.get("Location", ""))

        self.client.get("/agent/setup-2fa")
        with self.client.session_transaction() as session_state:
            secret = str(session_state.get("ngf_agent_pending_totp_secret") or "")
            self.assertTrue(secret)

        verification_code = generate_totp_code(secret)
        setup_response = self.client.post(
            "/agent/setup-2fa",
            data={"verification_code": verification_code},
            follow_redirects=True,
        )

        self.assertEqual(setup_response.status_code, 200)
        self.assertIn(b"backup-code", setup_response.data)
        updated = agent_store.get_user_by_email("agent@example.com")
        self.assertTrue(updated)
        self.assertTrue(updated.get("two_factor_enabled"))

    def test_failed_logins_lock_agent_account_after_five_attempts(self):
        self._create_user(email="locked@example.com")

        final_response = None
        for _ in range(5):
            final_response = self.client.post(
                "/agent/login",
                data={"email": "locked@example.com", "password": "wrong-pass"},
                follow_redirects=True,
            )

        self.assertIsNotNone(final_response)
        self.assertEqual(final_response.status_code, 200)
        self.assertIn(b"temporarily locked", final_response.data)
        locked_user = agent_store.get_user_by_email("locked@example.com")
        self.assertTrue(agent_store.is_locked(locked_user))

    def test_agent_logout_redirects_to_signed_out_login(self):
        secret = generate_totp_secret()
        self._create_user(
            email="signedout@example.com",
            password="signed-pass-123",
            role="agent_user",
            two_factor_enabled=True,
            secret=secret,
        )

        self.client.post(
            "/agent/login",
            data={"email": "signedout@example.com", "password": "signed-pass-123"},
            follow_redirects=False,
        )
        self.client.post(
            "/agent/verify-2fa",
            data={"verification_code": generate_totp_code(secret)},
            follow_redirects=False,
        )

        logout_response = self.client.post("/agent/logout", follow_redirects=False)
        self.assertEqual(logout_response.status_code, 302)
        self.assertIn("/agent/login?signed_out=1", logout_response.headers.get("Location", ""))

    def test_super_admin_can_disable_agent_and_block_future_login(self):
        admin_secret = generate_totp_secret()
        self._create_user(
            email="boss@example.com",
            password="boss-pass-123",
            role="super_admin",
            two_factor_enabled=True,
            secret=admin_secret,
        )
        target = self._create_user(email="target@example.com")

        password_step = self.client.post(
            "/agent/login",
            data={"email": "boss@example.com", "password": "boss-pass-123"},
            follow_redirects=False,
        )
        self.assertEqual(password_step.status_code, 302)
        self.assertIn("/agent/verify-2fa", password_step.headers.get("Location", ""))

        verify_step = self.client.post(
            "/agent/verify-2fa",
            data={"verification_code": generate_totp_code(admin_secret)},
            follow_redirects=True,
        )
        self.assertEqual(verify_step.status_code, 200)
        self.assertIn(b"Agent Dashboard", verify_step.data)

        disable_response = self.client.post(
            f"/agent/admin/users/{int(target.get('id') or 0)}/disable",
            data={"reason": "Fraud review"},
            follow_redirects=True,
        )
        self.assertEqual(disable_response.status_code, 200)
        self.assertIn(b"Disabled target@example.com", disable_response.data)

        self.client.post("/agent/logout", follow_redirects=True)
        blocked_login = self.client.post(
            "/agent/login",
            data={"email": "target@example.com", "password": "agent-pass-123"},
            follow_redirects=True,
        )
        self.assertEqual(blocked_login.status_code, 200)
        self.assertIn(b"disabled", blocked_login.data.lower())

    def test_backup_code_can_complete_two_factor_login(self):
        secret = generate_totp_secret()
        user = self._create_user(
            email="backuptest@example.com",
            two_factor_enabled=True,
            secret=secret,
        )
        # Pre-load 8 backup codes into the DB
        backup_codes = generate_backup_codes(8)
        backup_hashes = [hash_backup_code(c) for c in backup_codes]
        agent_store.update_two_factor(
            int(user["id"]),
            secret=secret,
            backup_code_hashes=backup_hashes,
        )

        self.client.post(
            "/agent/login",
            data={"email": "backuptest@example.com", "password": "agent-pass-123"},
            follow_redirects=False,
        )

        backup_code = backup_codes[0]
        response = self.client.post(
            "/agent/verify-2fa",
            data={"verification_code": backup_code},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Agent Dashboard", response.data)

        # Used code must be consumed — second use must fail
        self.client.post("/agent/logout", follow_redirects=True)
        self.client.post(
            "/agent/login",
            data={"email": "backuptest@example.com", "password": "agent-pass-123"},
            follow_redirects=False,
        )
        reuse_response = self.client.post(
            "/agent/verify-2fa",
            data={"verification_code": backup_code},
            follow_redirects=True,
        )
        self.assertIn(b"Invalid", reuse_response.data)

    def test_admin_cannot_disable_their_own_account(self):
        admin_secret = generate_totp_secret()
        admin_user = self._create_user(
            email="selfadmin@example.com",
            password="admin-pass-123",
            role="super_admin",
            two_factor_enabled=True,
            secret=admin_secret,
        )

        self.client.post(
            "/agent/login",
            data={"email": "selfadmin@example.com", "password": "admin-pass-123"},
            follow_redirects=False,
        )
        self.client.post(
            "/agent/verify-2fa",
            data={"verification_code": generate_totp_code(admin_secret)},
            follow_redirects=True,
        )

        response = self.client.post(
            f"/agent/admin/users/{int(admin_user['id'])}/disable",
            data={"reason": "Self-disable attempt"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"cannot disable your own", response.data)
        still_active = agent_store.get_user_by_email("selfadmin@example.com")
        self.assertTrue(still_active and still_active.get("is_active"))

    def test_super_admin_can_create_agent_from_dashboard(self):
        admin_secret = generate_totp_secret()
        self._create_user(
            email="boss@example.com",
            password="boss-pass-123",
            role="super_admin",
            two_factor_enabled=True,
            secret=admin_secret,
        )
        agency = agent_store.create_agency("Skyline Travel", code="skyline-travel")

        self.client.post(
            "/agent/login",
            data={"email": "boss@example.com", "password": "boss-pass-123"},
            follow_redirects=False,
        )
        self.client.post(
            "/agent/verify-2fa",
            data={"verification_code": generate_totp_code(admin_secret)},
            follow_redirects=True,
        )

        response = self.client.post(
            "/agent/admin/users/create",
            data={
                "first_name": "Nadia",
                "last_name": "Agent",
                "email": "nadia@example.com",
                "role": "agent_user",
                "agency_id": str(int(agency.get("id") or 0)),
                "new_agency_name": "",
                "password": "Agentpass123",
                "password_confirm": "Agentpass123",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Created agent login for nadia@example.com", response.data)
        created = agent_store.get_user_by_email("nadia@example.com")
        self.assertTrue(created)
        self.assertEqual(created.get("agency_name"), "Skyline Travel")
        self.assertEqual(created.get("effective_role"), "agent_user")

    def test_create_agent_rejects_duplicate_email(self):
        admin_secret = generate_totp_secret()
        self._create_user(
            email="boss@example.com",
            password="boss-pass-123",
            role="super_admin",
            two_factor_enabled=True,
            secret=admin_secret,
        )
        agency = agent_store.create_agency("Skyline Travel", code="skyline-travel")
        self._create_user(email="nadia@example.com")

        self.client.post(
            "/agent/login",
            data={"email": "boss@example.com", "password": "boss-pass-123"},
            follow_redirects=False,
        )
        self.client.post(
            "/agent/verify-2fa",
            data={"verification_code": generate_totp_code(admin_secret)},
            follow_redirects=True,
        )

        response = self.client.post(
            "/agent/admin/users/create",
            data={
                "first_name": "Nadia",
                "last_name": "Agent",
                "email": "nadia@example.com",
                "role": "agent_user",
                "agency_id": str(int(agency.get("id") or 0)),
                "new_agency_name": "",
                "password": "Agentpass123",
                "password_confirm": "Agentpass123",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"already exists", response.data)

    def test_agency_admin_request_approval_creates_requester_notification(self):
        reviewer_secret = generate_totp_secret()
        requester_secret = generate_totp_secret()
        self._create_user(
            email="reviewer@example.com",
            password="review-pass-123",
            role="super_admin",
            two_factor_enabled=True,
            secret=reviewer_secret,
        )
        requester = self._create_user(
            email="agencyadmin@example.com",
            password="agency-pass-123",
            role="agency_admin",
            two_factor_enabled=True,
            secret=requester_secret,
        )

        self._login_agent(email="agencyadmin@example.com", password="agency-pass-123", secret=requester_secret)
        request_response = self.client.post(
            "/agent/requests/new",
            data={
                "first_name": "Nina",
                "last_name": "Desk",
                "email": "nina@example.com",
                "role": "agent_user",
                "notes": "Needs booking access",
            },
            follow_redirects=True,
        )
        self.assertEqual(request_response.status_code, 200)
        self.assertIn(b"submitted", request_response.data)
        requests_list = agent_store.list_user_requests(status="pending", limit=10)
        self.assertEqual(len(requests_list), 1)
        request_id = int(requests_list[0]["id"])

        reviewer_notifications = agent_store.list_notifications(
            int(agent_store.get_user_by_email("reviewer@example.com")["id"]),
            limit=10,
        )
        self.assertTrue(any(n["type"] == "user_request_submitted" for n in reviewer_notifications))

        self.client.post("/agent/logout", follow_redirects=True)
        self._login_agent(email="reviewer@example.com", password="review-pass-123", secret=reviewer_secret)
        approve_response = self.client.post(
            f"/agent/requests/{request_id}/approve",
            data={
                "initial_password": "Agentpass123",
                "review_note": "Approved for onboarding",
            },
            follow_redirects=True,
        )
        self.assertEqual(approve_response.status_code, 200)
        requester_notifications = agent_store.list_notifications(int(requester["id"]), limit=10)
        self.assertTrue(any(n["type"] == "user_request_approved" for n in requester_notifications))
        approved_note = next(n for n in requester_notifications if n["type"] == "user_request_approved")
        self.assertIn("Approved for onboarding", approved_note["body"])

    def test_agency_admin_request_rejection_creates_requester_notification(self):
        reviewer_secret = generate_totp_secret()
        requester_secret = generate_totp_secret()
        self._create_user(
            email="reviewer@example.com",
            password="review-pass-123",
            role="super_admin",
            two_factor_enabled=True,
            secret=reviewer_secret,
        )
        requester = self._create_user(
            email="agencyadmin@example.com",
            password="agency-pass-123",
            role="agency_admin",
            two_factor_enabled=True,
            secret=requester_secret,
        )

        self._login_agent(email="agencyadmin@example.com", password="agency-pass-123", secret=requester_secret)
        self.client.post(
            "/agent/requests/new",
            data={
                "first_name": "Maya",
                "last_name": "Desk",
                "email": "maya@example.com",
                "role": "agent_user",
                "notes": "",
            },
            follow_redirects=True,
        )
        request_id = int(agent_store.list_user_requests(status="pending", limit=10)[0]["id"])
        self.client.post("/agent/logout", follow_redirects=True)

        self._login_agent(email="reviewer@example.com", password="review-pass-123", secret=reviewer_secret)
        reject_response = self.client.post(
            f"/agent/requests/{request_id}/reject",
            data={"review_note": "Missing business justification"},
            follow_redirects=True,
        )
        self.assertEqual(reject_response.status_code, 200)
        requester_notifications = agent_store.list_notifications(int(requester["id"]), limit=10)
        self.assertTrue(any(n["type"] == "user_request_rejected" for n in requester_notifications))
        rejected_note = next(n for n in requester_notifications if n["type"] == "user_request_rejected")
        self.assertIn("Missing business justification", rejected_note["body"])

    def test_dashboard_notifications_can_be_marked_read(self):
        secret = generate_totp_secret()
        user = self._create_user(
            email="notify@example.com",
            password="notify-pass-123",
            role="agency_admin",
            two_factor_enabled=True,
            secret=secret,
        )
        agent_store.create_notification(
            user_id=int(user["id"]),
            notification_type="user_request_approved",
            title="Approved",
            body="Your request was approved.",
        )
        self._login_agent(email="notify@example.com", password="notify-pass-123", secret=secret)

        dashboard = self.client.get("/agent/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"notifBell", dashboard.data)

        payload = self.client.get("/agent/notifications")
        self.assertEqual(payload.status_code, 200)
        self.assertEqual(payload.json["unread_count"], 1)

        marked = self.client.post("/agent/notifications/read", data={"_csrf": "ignored"})
        self.assertEqual(marked.status_code, 200)
        payload_after = self.client.get("/agent/notifications")
        self.assertEqual(payload_after.json["unread_count"], 0)
