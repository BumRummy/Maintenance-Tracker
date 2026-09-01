import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app


class MaintenanceIssueTests(unittest.TestCase):
    def make_app(self, config_path, timezone_name="UTC"):
        with patch.dict(os.environ, {"CONFIG_PATH": config_path, "TZ": timezone_name}):
            app = create_app()
        app.config.update(TESTING=True)
        return app

    def test_dates_are_displayed_in_configured_timezone(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path, "America/New_York")

            formatted = app.jinja_env.filters["fmtdate"]("2026-08-26T16:30:00+00:00")

            self.assertEqual(formatted, "Aug 26 2026 12:30")

    def test_pwa_assets_use_text_icon(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            client = app.test_client()

            manifest_response = client.get("/static/manifest.webmanifest")
            manifest = json.loads(manifest_response.data)
            icon_response = client.get(manifest["icons"][0]["src"])

            self.assertEqual(manifest_response.status_code, 200)
            self.assertEqual(manifest["display"], "standalone")
            self.assertEqual(manifest["icons"][0]["type"], "image/svg+xml")
            self.assertEqual(icon_response.status_code, 200)
            self.assertTrue(icon_response.data.startswith(b"<svg"))
            manifest_response.close()
            icon_response.close()

    def test_admin_can_add_locations_and_assign_users_from_the_location_list(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            client = app.test_client()
            with client.session_transaction() as session:
                session["user"] = "admin"
                session["role"] = "admin"

            response = client.post("/admin", data={"action": "add_location", "location": "Downtown"})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(app.config["STORE"].load_settings()["locations"], ["HCK", "Downtown"])

            response = client.post(
                "/admin",
                data={
                    "action": "add_user",
                    "username": "downtown-desk",
                    "email": "desk@example.com",
                    "password": "password",
                    "role": "front_desk",
                    "location": "Downtown",
                },
            )
            self.assertEqual(response.status_code, 302)
            user = next(user for user in app.config["STORE"].load_settings()["users"] if user["username"] == "downtown-desk")
            self.assertEqual(user["location"], "Downtown")

            admin_page = client.get("/admin")
            self.assertIn(b'<option value="HCK" selected>HCK</option>', admin_page.data)
            self.assertIn(b'<option value="Downtown" selected>Downtown</option>', admin_page.data)

    def test_maintenance_user_can_open_form_and_create_issue(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            client = app.test_client()
            with client.session_transaction() as session:
                session["user"] = "maintenance"
                session["role"] = "maintenance"

            dashboard = client.get("/dashboard")
            self.assertIn(b'aria-label="Add maintenance issue"', dashboard.data)

            response = client.post(
                "/issues",
                data={"room": "204", "description": "Sink is leaking"},
                follow_redirects=True,
            )

            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Issue submitted for room 204.", response.data)
            issue = app.config["STORE"].load_issues()[0]
            self.assertEqual(issue["created_by"], "maintenance")

    def test_maintenance_user_can_save_push_subscription(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            client = app.test_client()
            with client.session_transaction() as session:
                session["user"] = "maintenance"
                session["role"] = "maintenance"

            subscription = {
                "endpoint": "https://push.example/subscription",
                "keys": {"p256dh": "browser-key", "auth": "auth-secret"},
            }
            response = client.post("/api/push/subscriptions", json=subscription)

            self.assertEqual(response.status_code, 201)
            saved = app.config["STORE"].load_push_subscriptions()
            self.assertEqual(saved[0]["username"], "maintenance")
            self.assertEqual(saved[0]["subscription"], subscription)

    def test_push_config_automatically_creates_and_reuses_vapid_keys(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            client = app.test_client()
            with client.session_transaction() as session:
                session["user"] = "maintenance"
                session["role"] = "maintenance"

            first_public_key = client.get("/api/push/config").get_json()["publicKey"]
            second_app = self.make_app(config_path)

            self.assertTrue(first_public_key)
            self.assertEqual(second_app.config["VAPID_PUBLIC_KEY"], first_public_key)
            self.assertTrue(Path(config_path, "vapid_private.pem").is_file())

    def test_enabled_notification_control_uses_bell_icon(self):
        script = Path("static/push-notifications.js").read_text(encoding="utf-8")

        self.assertIn("Notifications enabled", script)
        self.assertIn("<svg", script)
        self.assertIn("showEnabled();", script)

    def test_creating_issue_sends_push_to_maintenance_subscription(self):
        with tempfile.TemporaryDirectory() as config_path:
            with patch.dict(
                os.environ,
                {
                    "CONFIG_PATH": config_path,
                    "TZ": "UTC",
                    "VAPID_PUBLIC_KEY": "public-key",
                    "VAPID_PRIVATE_KEY": "private-key",
                },
            ):
                app = create_app()
            app.config.update(TESTING=True)
            store = app.config["STORE"]
            store.save_push_subscription(
                {"endpoint": "https://push.example/subscription", "keys": {"p256dh": "key", "auth": "secret"}},
                "maintenance",
                "maintenance",
                "HCK",
            )
            client = app.test_client()
            with client.session_transaction() as session:
                session["user"] = "frontdesk"
                session["role"] = "front_desk"

            with patch("app.webpush") as send_push:
                response = client.post("/issues", data={"room": "305", "description": "Air conditioner stopped"})

            self.assertEqual(response.status_code, 302)
            send_push.assert_called_once()
            self.assertIn("Room 305", send_push.call_args.kwargs["data"])
            self.assertEqual(send_push.call_args.kwargs["ttl"], 86400)

    def test_location_assignment_isolated_for_issues_history_and_closing(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            store = app.config["STORE"]
            settings = store.load_settings()
            settings["users"] = [
                {"username": "north", "password": "password", "role": "maintenance", "email": "", "location": "North Hotel"},
                {"username": "south", "password": "password", "role": "maintenance", "email": "", "location": "South Hotel"},
            ]
            store.save_settings(settings)
            north_issue = store.add_issue("101", "North-only issue", "north", "North Hotel")
            south_issue = store.add_issue("201", "South-only issue", "south", "South Hotel")
            client = app.test_client()
            with client.session_transaction() as session:
                session["user"] = "north"
                session["role"] = "maintenance"

            dashboard = client.get("/dashboard")
            self.assertIn(b"North-only issue", dashboard.data)
            self.assertNotIn(b"South-only issue", dashboard.data)

            response = client.post(
                f"/issues/{south_issue['id']}/close",
                data={"resolution": "Should not be allowed"},
                follow_redirects=True,
            )
            self.assertIn(b"Issue was not found at your assigned location.", response.data)
            self.assertEqual(store.load_issues()[1]["status"], "open")

            client.post("/issues", data={"room": "102", "description": "New north issue"})
            created_issue = store.load_issues()[-1]
            self.assertEqual(created_issue["location"], "North Hotel")
            self.assertEqual(north_issue["location"], "North Hotel")

            client.post(f"/issues/{north_issue['id']}/close", data={"resolution": "Fixed at north"})
            history = client.get("/history")
            self.assertIn(b"North-only issue", history.data)
            self.assertNotIn(b"South-only issue", history.data)

    def test_supervisor_can_manage_only_local_non_admin_users_and_download_reports(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            store = app.config["STORE"]
            settings = store.load_settings()
            settings["locations"] = ["HCK", "Downtown"]
            settings["users"].extend(
                [
                    {"username": "local-supervisor", "password": "password", "role": "supervisor", "email": "", "location": "HCK"},
                    {"username": "downtown-desk", "password": "password", "role": "front_desk", "email": "", "location": "Downtown"},
                ]
            )
            store.save_settings(settings)
            store.add_issue("101", "HCK issue", "frontdesk", "HCK")
            store.add_issue("201", "Downtown issue", "downtown-desk", "Downtown")
            client = app.test_client()
            with client.session_transaction() as session:
                session["user"] = "local-supervisor"
                session["role"] = "supervisor"

            dashboard = client.get("/dashboard")
            self.assertIn(b'aria-label="Supervisor settings"', dashboard.data)
            page = client.get("/supervisor")
            self.assertIn(b"Managing users and reports for HCK.", page.data)
            self.assertNotIn(b"downtown-desk", page.data)

            response = client.post(
                "/supervisor",
                data={"action": "add_user", "username": "new-desk", "email": "desk@example.com", "password": "password", "role": "front_desk"},
            )
            self.assertEqual(response.status_code, 302)
            new_user = next(user for user in store.load_settings()["users"] if user["username"] == "new-desk")
            self.assertEqual(new_user["location"], "HCK")

            response = client.post("/supervisor", data={"action": "delete_user", "username": "downtown-desk"}, follow_redirects=True)
            self.assertIn(b"User was not found at your location.", response.data)
            self.assertTrue(any(user["username"] == "downtown-desk" for user in store.load_settings()["users"]))

            report = client.get("/supervisor/report.csv")
            self.assertEqual(report.status_code, 200)
            self.assertEqual(report.mimetype, "text/csv")
            self.assertIn(b"HCK issue", report.data)
            self.assertNotIn(b"Downtown issue", report.data)


if __name__ == "__main__":
    unittest.main()
