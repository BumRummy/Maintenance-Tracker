import json
import os
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
