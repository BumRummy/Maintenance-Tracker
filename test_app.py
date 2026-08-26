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


if __name__ == "__main__":
    unittest.main()
