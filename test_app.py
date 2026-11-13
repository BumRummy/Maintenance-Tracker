import json
from datetime import datetime, timezone
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import check_password_hash

from app import Store, create_app, hash_password, is_password_hash


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

    def test_admin_can_change_an_existing_users_role(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            client = app.test_client()
            with client.session_transaction() as session:
                session["user"] = "admin"
                session["role"] = "admin"

            response = client.post(
                "/admin",
                data={"action": "change_role", "username": "frontdesk", "role": "maintenance"},
                follow_redirects=True,
            )

            user = next(user for user in app.config["STORE"].load_settings()["users"] if user["username"] == "frontdesk")
            self.assertEqual(user["role"], "maintenance")
            self.assertIn(b"Role updated for &#39;frontdesk&#39;.", response.data)

    def test_admin_cannot_assign_an_invalid_or_admin_role(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            client = app.test_client()
            with client.session_transaction() as session:
                session["user"] = "admin"
                session["role"] = "admin"

            response = client.post(
                "/admin",
                data={"action": "change_role", "username": "frontdesk", "role": "admin"},
                follow_redirects=True,
            )

            user = next(user for user in app.config["STORE"].load_settings()["users"] if user["username"] == "frontdesk")
            self.assertEqual(user["role"], "front_desk")
            self.assertIn(b"Select a valid role.", response.data)

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

    def test_issue_categories_tabs_and_checkout_date(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            client = app.test_client()
            with client.session_transaction() as session:
                session["user"] = "frontdesk"
                session["role"] = "front_desk"

            # Front desk defaults to the guest tab and guest category.
            page = client.get("/dashboard").data
            self.assertIn(b'value="guest" checked', page)
            client.post("/issues", data={"room": "101", "description": "Needs extra towels"})
            client.post(
                "/issues",
                data={"room": "102", "description": "Fix door", "category": "maintenance"},
            )
            client.post(
                "/issues",
                data={
                    "room": "104", "description": "Swap pillows", "category": "guest",
                    "do_on_checkout": "on", "checkout_date": "2026-11-05",
                },
            )
            # Checkout dates are ignored for maintenance issues.
            client.post(
                "/issues",
                data={
                    "room": "105", "description": "Paint wall", "category": "maintenance",
                    "do_on_checkout": "on", "checkout_date": "2026-11-06",
                },
            )
            issues = {i["room"]: i for i in app.config["STORE"].load_issues()}
            self.assertEqual(issues["101"]["category"], "guest")
            self.assertEqual(issues["102"]["category"], "maintenance")
            self.assertIsNone(issues["102"]["checkout_date"])
            self.assertEqual(issues["104"]["checkout_date"], "2026-11-05")
            self.assertIsNone(issues["105"]["checkout_date"])

            # Both categories render as separate columns on one page.
            page = client.get("/dashboard").data.decode()
            columns = page.split('<section data-category=')[1:]
            self.assertEqual(len(columns), 2)
            guest, maint = columns
            self.assertIn("Needs extra towels", guest)
            self.assertNotIn("Fix door", guest)
            self.assertIn("2026-11-05", guest)
            self.assertIn("Fix door", maint)
            self.assertNotIn("Needs extra towels", maint)
            self.assertNotIn("2026-11-06", page)

            # Sorting by room number, remembered for the session.
            client.post("/issues", data={"room": "9", "description": "Late room", "category": "guest"})
            client.post("/issues", data={"room": "10", "description": "Later room", "category": "guest"})
            default_page = client.get("/dashboard").data.decode()
            self.assertLess(default_page.index("Needs extra towels"), default_page.index("Late room"))
            self.assertLess(default_page.index("Late room"), default_page.index("Later room"))
            by_room = client.get("/dashboard?sort=room").data.decode()
            self.assertLess(by_room.index("Late room"), by_room.index("Later room"))
            self.assertLess(by_room.index("Later room"), by_room.index("Needs extra towels"))
            self.assertIn("Later room", client.get("/dashboard").data.decode())
            client.get("/dashboard?sort=default")
            for room in ("9", "10"):
                issue = next(i for i in app.config["STORE"].load_issues() if i["room"] == room)
                app.config["STORE"].close_issue(issue["id"], "maintenance", "done")

            # Several issues for one room can be submitted at once.
            before = len(app.config["STORE"].load_issues())
            multi = client.post(
                "/issues",
                data={
                    "room": "220",
                    "row": ["1", "2"],
                    "description_1": "TV remote missing", "category_1": "guest",
                    "do_on_checkout_1": "on", "checkout_date_1": "2026-12-01",
                    "description_2": "Closet door off track", "category_2": "maintenance",
                },
                follow_redirects=True,
            )
            self.assertIn(b"2 issues submitted for room 220.", multi.data)
            added = app.config["STORE"].load_issues()[before:]
            self.assertEqual([(i["room"], i["category"], i["checkout_date"]) for i in added],
                             [("220", "guest", "2026-12-01"), ("220", "maintenance", None)])
            # A blank description rejects the whole submission.
            client.post("/issues", data={"room": "221", "row": ["1", "2"], "description_1": "ok", "description_2": " "})
            self.assertEqual(len(app.config["STORE"].load_issues()), before + 2)
            for issue in added:
                app.config["STORE"].close_issue(issue["id"], "maintenance", "done")

            # Open issues can be edited by any dashboard role.
            target = next(i for i in app.config["STORE"].load_issues() if i["room"] == "104")
            edited = client.post(
                f"/issues/{target['id']}/edit",
                data={"room": "105a", "description": "Swap feather pillows", "category": "guest",
                      "do_on_checkout": "on", "checkout_date": "2026-11-20"},
            )
            self.assertEqual(edited.get_json()["issue"]["room"], "105A")
            saved = next(i for i in app.config["STORE"].load_issues() if i["id"] == target["id"])
            self.assertEqual((saved["description"], saved["checkout_date"], saved["edited_by"]),
                             ("Swap feather pillows", "2026-11-20", "frontdesk"))
            as_maint = client.post(f"/issues/{target['id']}/edit", data={"room": "105A", "description": "x", "category": "maintenance"})
            self.assertEqual(as_maint.status_code, 200)
            saved = next(i for i in app.config["STORE"].load_issues() if i["id"] == target["id"])
            self.assertIsNone(saved["checkout_date"])
            for bad in ({"room": "", "description": "x", "category": "guest"},
                        {"room": "1", "description": "x", "category": "nope"},
                        {"room": "1", "description": "x", "category": "guest", "do_on_checkout": "on"}):
                self.assertEqual(client.post(f"/issues/{target['id']}/edit", data=bad).status_code, 400)
            self.assertEqual(client.post("/issues/missing/edit", data={"room": "1", "description": "x", "category": "guest"}).status_code, 404)
            store = app.config["STORE"]
            all_issues = store.load_issues()
            for i in all_issues:
                if i["id"] == target["id"]:
                    i["room"] = "104"
                    i["category"] = "guest"
                    i["checkout_date"] = "2026-11-05"
            store.save_issues(all_issues)

            # Supervisors and maintenance can move issues; front desk cannot.
            issue_id = next(i["id"] for i in app.config["STORE"].load_issues() if i["room"] == "104")
            denied = client.post(f"/issues/{issue_id}/move", data={"category": "maintenance"})
            self.assertEqual(denied.status_code, 403)
            for role in ("supervisor", "maintenance"):
                with client.session_transaction() as session:
                    session["user"] = role
                    session["role"] = role
                moved = client.post(f"/issues/{issue_id}/move", data={"category": "maintenance"})
                self.assertEqual(moved.get_json()["category"], "maintenance")
                issue = next(i for i in app.config["STORE"].load_issues() if i["id"] == issue_id)
                self.assertIsNone(issue["checkout_date"])
                self.assertEqual(client.post(f"/issues/{issue_id}/move", data={"category": "bogus"}).status_code, 400)
                client.post(f"/issues/{issue_id}/move", data={"category": "guest"})
            with client.session_transaction() as session:
                session["user"] = "frontdesk"
                session["role"] = "front_desk"

            # Checkout requires a valid date.
            client.post("/issues", data={"room": "103", "description": "x", "category": "guest", "do_on_checkout": "on"})
            self.assertEqual(len(app.config["STORE"].load_issues()), 8)

            with client.session_transaction() as session:
                session["user"] = "supervisor"
                session["role"] = "supervisor"
            self.assertIn(b'value="guest" checked', client.get("/dashboard").data)

    def test_room_issues_lists_open_and_history_for_the_users_location(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            client = app.test_client()
            store = app.config["STORE"]
            store.add_issue("101", "Leaky sink", "frontdesk", "HCK", "maintenance")
            store.add_issue("101", "Extra towels", "frontdesk", "HCK", "guest", "2026-12-01")
            old = store.add_issue("101", "Broken lamp", "frontdesk", "HCK")
            store.close_issue(old["id"], "maintenance", "Replaced bulb")
            store.add_issue("102", "Other room", "frontdesk", "HCK")
            store.add_issue("101", "Other hotel", "frontdesk", "HCK2")

            self.assertEqual(client.get("/api/room-issues?room=101").status_code, 403)
            with client.session_transaction() as session:
                session["user"] = "frontdesk"
                session["role"] = "front_desk"
            data = client.get("/api/room-issues?room=101").get_json()
            self.assertEqual(sorted(i["description"] for i in data["open"]), ["Extra towels", "Leaky sink"])
            self.assertEqual([i["resolution"] for i in data["history"]], ["Replaced bulb"])
            self.assertEqual(client.get("/api/room-issues?room=").status_code, 400)
            self.assertIn(b'data-room="101"', client.get("/dashboard").data)

    def test_completing_an_issue_via_fetch_returns_json_and_plain_post_redirects(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            client = app.test_client()
            store = app.config["STORE"]
            first = store.add_issue("101", "One", "frontdesk", "HCK")
            second = store.add_issue("102", "Two", "frontdesk", "HCK")
            with client.session_transaction() as session:
                session["user"] = "maintenance"
                session["role"] = "maintenance"

            blank = client.post(f"/issues/{first['id']}/close", data={"resolution": " "}, headers={"X-Requested-With": "fetch"})
            self.assertEqual(blank.status_code, 400)
            done = client.post(f"/issues/{first['id']}/close", data={"resolution": "Fixed"}, headers={"X-Requested-With": "fetch"})
            self.assertEqual(done.get_json(), {"ok": True})
            again = client.post(f"/issues/{first['id']}/close", data={"resolution": "Fixed"}, headers={"X-Requested-With": "fetch"})
            self.assertEqual(again.status_code, 404)
            plain = client.post(f"/issues/{second['id']}/close", data={"resolution": "Fixed"})
            self.assertEqual(plain.status_code, 302)
            self.assertTrue(all(i["status"] == "closed" for i in store.load_issues()))

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

    def test_supervisor_csv_reports_are_scoped_to_exact_hotel_names(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            store = app.config["STORE"]
            settings = store.load_settings()
            settings["locations"] = ["HCK", "HCK2"]
            settings["users"].extend(
                [
                    {"username": "hck-supervisor", "password": "password", "role": "supervisor", "email": "", "location": "HCK"},
                    {"username": "hck2-supervisor", "password": "password", "role": "supervisor", "email": "", "location": "HCK2"},
                ]
            )
            store.save_settings(settings)
            store.add_issue("101", "HCK-only issue", "frontdesk", "HCK")
            store.add_issue("201", "HCK2-only issue", "frontdesk", "HCK2")
            client = app.test_client()

            for username, expected_issue, excluded_issue in (
                ("hck-supervisor", b"HCK-only issue", b"HCK2-only issue"),
                ("hck2-supervisor", b"HCK2-only issue", b"HCK-only issue"),
            ):
                with client.session_transaction() as session:
                    session["user"] = username
                    session["role"] = "supervisor"

                report = client.get("/supervisor/report.csv")

                self.assertEqual(report.status_code, 200)
                self.assertIn(expected_issue, report.data)
                self.assertNotIn(excluded_issue, report.data)

    def test_privacy_and_support_pages_are_public(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            client = app.test_client()
            with patch.dict(os.environ, {"SUPPORT_EMAIL": "help@example.com"}):
                privacy = client.get("/privacy")
                support = client.get("/support")

            self.assertEqual(privacy.status_code, 200)
            self.assertIn(b"never uploaded", privacy.data)
            self.assertEqual(support.status_code, 200)
            self.assertIn(b"help@example.com", support.data)

    def test_api_work_and_parts_on_issues_saved_before_locations_existed(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            store = app.config["STORE"]
            issue = store.add_issue("101", "Leak", "frontdesk", "HCK", "maintenance")
            issues = store.load_issues()
            del issues[0]["location"]  # older records have no location; the site treats them as HCK
            store.save_issues(issues)
            client = app.test_client()
            self.login(client, "maintenance")
            item = client.post("/api/v1/inventory", json={"name": "Washer", "qty": 2}).get_json()

            started = client.post(f"/api/v1/issues/{issue['id']}/events", json={"type": "start"})
            part = client.post(f"/api/v1/issues/{issue['id']}/parts", json={"item_id": item["id"]})

            self.assertEqual(started.status_code, 200)
            self.assertEqual(part.status_code, 200)

    def test_api_property_settings_are_per_location_and_supervisor_only(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            client = app.test_client()

            self.login(client, "maintenance")
            self.assertEqual(client.get("/api/v1/property").get_json()["rooms"], [])
            self.assertEqual(client.put("/api/v1/property", json={"name": "X"}).status_code, 403)
            client.post("/api/v1/logout")

            self.login(client, "supervisor")
            saved = client.put("/api/v1/property", json={
                "name": "Hampton Inn", "address": "1 Main St", "phone": "555-0100",
                "rooms": ["214", " 1102 ", "98", "214", "305b", ""],
            })
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.get_json()["rooms"], ["98", "214", "305B", "1102"])
            self.assertEqual(client.put("/api/v1/property", json={"rooms": ["2 14"]}).status_code, 400)
            # Updating one field leaves the rest alone.
            renamed = client.put("/api/v1/property", json={"name": "Hampton Inn HCK"}).get_json()
            self.assertEqual(renamed["address"], "1 Main St")
            self.assertEqual(len(renamed["rooms"]), 4)

            settings = app.config["STORE"].load_settings()
            settings["users"].append({"username": "other", "password": hash_password("changeme"), "role": "supervisor",
                                      "location": "HCK2", "email": "o@example.com", "force_password_change": False})
            app.config["STORE"].save_settings(settings)
            client.post("/api/v1/logout")
            self.login(client, "other")
            self.assertEqual(client.get("/api/v1/property").get_json()["rooms"], [])

    def test_api_supervisor_manages_users_at_own_location(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            client = app.test_client()
            self.login(client, "maintenance")
            self.assertEqual(client.get("/api/v1/users").status_code, 403)
            client.post("/api/v1/logout")
            self.login(client, "supervisor")

            names = [u["username"] for u in client.get("/api/v1/users").get_json()["users"]]
            self.assertNotIn("admin", names)
            self.assertEqual(client.post("/api/v1/users", json={
                "username": "maria", "email": "m@example.com", "role": "admin", "password": "temporary1",
            }).status_code, 400)
            added = client.post("/api/v1/users", json={
                "username": "Maria", "email": "m@example.com", "role": "maintenance", "password": "temporary1",
            })
            self.assertEqual(added.status_code, 201)
            self.assertTrue(added.get_json()["must_change_password"])
            self.assertEqual(client.post("/api/v1/users", json={
                "username": "maria", "email": "x@example.com", "role": "front_desk", "password": "temporary1",
            }).status_code, 400)
            self.assertEqual(client.patch("/api/v1/users/maria", json={"role": "front_desk"}).get_json()["role"], "front_desk")
            self.assertEqual(client.patch("/api/v1/users/supervisor", json={"role": "maintenance"}).status_code, 400)
            self.assertEqual(client.patch("/api/v1/users/admin", json={"role": "maintenance"}).status_code, 404)
            self.assertEqual(client.delete("/api/v1/users/supervisor").status_code, 400)
            self.assertEqual(client.delete("/api/v1/users/maria").status_code, 200)
            self.assertNotIn("maria", [u["username"] for u in client.get("/api/v1/users").get_json()["users"]])

    def test_urgent_pm_from_a_supervisor_goes_to_guest_issues(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            client = app.test_client()
            self.login(client, "maintenance")
            denied = client.post("/api/v1/issues", json={
                "room": "214", "issues": [{"description": "Leak", "category": "maintenance", "urgent": True}],
            })
            self.assertEqual(denied.status_code, 403)
            client.post("/api/v1/logout")

            self.login(client, "supervisor")
            created = client.post("/api/v1/issues", json={
                "room": "214", "issues": [
                    {"description": "Ceiling leak", "category": "maintenance", "urgent": True, "checkout_date": "2026-11-20"},
                    {"description": "Filter", "category": "maintenance"},
                ],
            }).get_json()["issues"]
            self.assertEqual((created[0]["category"], created[0]["urgent"], created[0]["checkout_date"]), ("guest", True, None))
            self.assertEqual((created[1]["category"], created[1]["urgent"]), ("maintenance", False))

            # Same on the website form.
            with client.session_transaction() as session:
                session["user"] = "supervisor"
                session["role"] = "supervisor"
            client.post("/issues", data={"room": "305", "row": "1", "description_1": "No heat",
                                         "category_1": "maintenance", "urgent_1": "on"})
            web = next(i for i in app.config["STORE"].load_issues() if i["room"] == "305")
            self.assertEqual((web["category"], web["urgent"]), ("guest", True))

    def test_new_requests_notify_iphones_at_the_same_location(self):
        class FakeSender:
            def __init__(self):
                self.sent = []

            def send(self, client, token, payload, sandbox):
                self.sent.append((token, payload, sandbox))
                return (410, "Unregistered") if token == "dead" else (200, "")

        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            sender = FakeSender()
            app.config.update(APNS_SENDER=sender, APNS_SYNC=True)
            self.use_known_passwords(app)
            client = app.test_client()

            self.login(client, "maintenance")
            self.assertEqual(client.post("/api/v1/devices", json={"token": "not hex!"}).status_code, 400)
            ok = client.post("/api/v1/devices", json={"token": "AB12", "sandbox": True}).get_json()
            self.assertTrue(ok["notifications"])
            client.post("/api/v1/devices", json={"token": "de", "sandbox": False})
            client.post("/api/v1/logout")
            self.login(client, "frontdesk")  # front desk phones aren't alerted
            client.post("/api/v1/devices", json={"token": "fd"})
            from apns import Devices
            Devices(config_path).register("dead", "maintenance", "maintenance", "HCK", False)
            Devices(config_path).register("cc", "other", "maintenance", "HCK2", False)

            client.post("/api/v1/issues", json={"room": "214", "issues": [{"description": "Leak", "category": "guest"}]})

            self.assertEqual(sorted(t for t, _, _ in sender.sent), ["ab12", "de", "dead"])
            token, payload, sandbox = next(x for x in sender.sent if x[0] == "ab12")
            self.assertTrue(sandbox)
            self.assertEqual(payload["aps"]["alert"]["title"], "New job · Room 214")
            self.assertNotIn("dead", [d["token"] for d in Devices(config_path).load()])

            client.post("/api/v1/logout")
            self.login(client, "maintenance")
            client.delete("/api/v1/devices/AB12")
            self.assertNotIn("ab12", [d["token"] for d in Devices(config_path).load()])

    def test_apns_provider_token_is_signed_with_the_auth_key(self):
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from apns import APNsSender

        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        sender = APNsSender(pem, "KEY1234567", "TEAM123456", "Com.HotelCraft.Maintenance")
        token = sender.provider_token()

        self.assertEqual(jwt.get_unverified_header(token)["kid"], "KEY1234567")
        claims = jwt.decode(token, key.public_key(), algorithms=["ES256"])
        self.assertEqual(claims["iss"], "TEAM123456")
        self.assertIs(sender.provider_token(), token)  # reused until it nears expiry

    def test_quarterly_pm_checklist(self):
        from datetime import date
        from pm import DEFAULT_PM_ITEMS

        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            client = app.test_client()
            self.login(client, "frontdesk")
            self.assertEqual(client.get("/api/v1/pm").status_code, 403)
            client.post("/api/v1/logout")
            self.login(client, "supervisor")
            client.put("/api/v1/property", json={"rooms": ["115", "119", "210"]})
            pm_request = app.config["STORE"].add_issue("119", "Re-caulk tub", "frontdesk", "HCK", "maintenance")
            app.config["STORE"].add_issue("305", "Loose towel bar", "frontdesk", "HCK", "maintenance")
            client.post("/api/v1/logout")
            self.login(client, "maintenance")

            with patch("api.datetime") as fake_datetime:
                fake_datetime.now.return_value = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
                fake_datetime.strptime = datetime.strptime
                quarter = client.get("/api/v1/pm").get_json()
                self.assertEqual((quarter["year"], quarter["quarter"], quarter["ends"]), (2026, 4, "2026-12-31"))
                self.assertEqual(len(quarter["items"]), len(DEFAULT_PM_ITEMS))
                rooms = {r["room"]: r for r in quarter["rooms"]}
                self.assertEqual(list(rooms), ["115", "119", "210", "305"])  # 305 has a PM request
                self.assertEqual(rooms["119"]["open_requests"], 1)
                self.assertFalse(rooms["305"]["on_room_list"])
                self.assertEqual(client.get("/api/v1/pm?year=2027&quarter=1").status_code, 400)

                partial = client.put("/api/v1/pm/rooms/119/2026/4", json={"ratings": [{"item": "Mirror", "rating": 3}]})
                self.assertEqual(partial.status_code, 200)
                self.assertEqual(client.put("/api/v1/pm/rooms/119/2026/4", json={
                    "ratings": {"Mirror": 3}, "complete": True}).status_code, 400)
                ratings = {item: 3 for item in DEFAULT_PM_ITEMS}
                ratings["Switches / Cover Plates"] = 2
                done = client.put("/api/v1/pm/rooms/119/2026/4", json={
                    "ratings": ratings, "notes": "Replaced switch plate", "complete": True,
                    "close_request_ids": [pm_request["id"]],
                }).get_json()
                self.assertEqual(done["quarters"]["4"]["date"], "2026-11-13")
                self.assertIn({"item": "Switches / Cover Plates", "rating": 2}, done["quarters"]["4"]["ratings"])
                self.assertEqual(done["open_requests"], [])
                # Paper records from earlier quarters can be entered too.
                self.assertEqual(client.put("/api/v1/pm/rooms/119/2026/1", json={
                    "ratings": ratings, "complete": True, "date": "2026-03-07"}).status_code, 200)

                rooms = {r["room"]: r for r in client.get("/api/v1/pm").get_json()["rooms"]}
                self.assertEqual(rooms["119"]["status"], "done")
                self.assertEqual(rooms["115"]["status"], "due")
                closed = next(i for i in app.config["STORE"].load_issues() if i["id"] == pm_request["id"])
                self.assertEqual(closed["resolution"], "Done during Q4 2026 PM.")
                detail = client.get("/api/v1/pm/rooms/119").get_json()
                self.assertEqual(detail["quarters"]["1"]["date"], "2026-03-07")
                self.assertIsNone(detail["quarters"]["2"])

    def use_known_passwords(self, app):
        """Fresh deployments seed random passwords; give every account "changeme" for API tests."""
        store = app.config["STORE"]
        settings = store.load_settings()
        for user in settings["users"]:
            user["password"] = hash_password("changeme")
            user["force_password_change"] = False
        store.save_settings(settings)

    def test_api_login_issue_lifecycle_and_location_scoping(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            client = app.test_client()

            self.assertEqual(client.get("/api/v1/issues").status_code, 401)
            bad = client.post("/api/v1/login", json={"username": "maintenance", "password": "nope"})
            self.assertEqual(bad.status_code, 401)

            login = client.post("/api/v1/login", json={"username": "Maintenance", "password": "changeme"})
            self.assertEqual(login.status_code, 200)
            self.assertEqual(login.get_json()["role"], "maintenance")
            self.assertEqual(login.get_json()["location"], "HCK")

            created = client.post("/api/v1/issues", json={
                "room": "214",
                "issues": [
                    {"description": "Sink drains slowly", "category": "maintenance"},
                    {"description": "Lamp out", "category": "guest", "checkout_date": "2026-11-20"},
                ],
            })
            self.assertEqual(created.status_code, 201)
            first, second = created.get_json()["issues"]
            self.assertEqual(second["checkout_date"], "2026-11-20")

            app.config["STORE"].add_issue("999", "Other hotel", "someone", "HCK2")
            listing = client.get("/api/v1/issues").get_json()
            self.assertEqual([i["room"] for i in listing["open"]], ["214", "214"])

            moved = client.post(f"/api/v1/issues/{second['id']}/move", json={"category": "maintenance"})
            self.assertEqual(moved.get_json()["category"], "maintenance")
            self.assertIsNone(moved.get_json()["checkout_date"])

            edited = client.patch(f"/api/v1/issues/{first['id']}", json={
                "room": "215", "description": "Sink clogged", "category": "maintenance",
            })
            self.assertEqual(edited.get_json()["room"], "215")
            self.assertEqual(edited.get_json()["edited_by"], "maintenance")

            missing = client.post(f"/api/v1/issues/{first['id']}/close", json={"resolution": " "})
            self.assertEqual(missing.status_code, 400)
            closed = client.post(f"/api/v1/issues/{first['id']}/close", json={"resolution": "Snaked drain"})
            self.assertEqual(closed.get_json()["status"], "closed")
            self.assertEqual(closed.get_json()["closed_by"], "maintenance")

            room = client.get("/api/v1/rooms/215").get_json()
            self.assertEqual(room["open"], [])
            self.assertEqual(room["history"][0]["resolution"], "Snaked drain")
            listing = client.get("/api/v1/issues").get_json()
            self.assertEqual(len(listing["open"]), 1)
            self.assertEqual(len(listing["recently_closed"]), 1)

            client.post("/api/v1/logout")
            self.assertEqual(client.get("/api/v1/me").status_code, 401)

    def test_api_front_desk_cannot_close_issues(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            client = app.test_client()
            issue = app.config["STORE"].add_issue("101", "Leak", "frontdesk", "HCK")
            client.post("/api/v1/login", json={"username": "frontdesk", "password": "changeme"})

            response = client.post(f"/api/v1/issues/{issue['id']}/close", json={"resolution": "Done"})

            self.assertEqual(response.status_code, 403)

    def test_api_password_change_clears_forced_change(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            store = app.config["STORE"]
            settings = store.load_settings()
            settings["users"][1]["force_password_change"] = True
            store.save_settings(settings)
            client = app.test_client()

            login = client.post("/api/v1/login", json={"username": "maintenance", "password": "changeme"})
            self.assertTrue(login.get_json()["must_change_password"])
            self.assertEqual(client.post("/api/v1/password", json={"new_password": "short"}).status_code, 400)
            changed = client.post("/api/v1/password", json={"new_password": "longenough1"})

            self.assertFalse(changed.get_json()["must_change_password"])

    def login(self, client, username):
        response = client.post("/api/v1/login", json={"username": username, "password": "changeme"})
        self.assertEqual(response.status_code, 200)

    def test_api_work_events_drive_state_and_metrics(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            client = app.test_client()
            issue = app.config["STORE"].add_issue("214", "PTAC filter", "frontdesk", "HCK", "maintenance")
            self.login(client, "maintenance")
            url = f"/api/v1/issues/{issue['id']}/events"

            self.assertEqual(client.post(url, json={"type": "stop"}).status_code, 400)
            self.assertEqual(client.post(url, json={"type": "start"}).get_json()["work_state"], "active")
            self.assertEqual(client.post(url, json={"type": "awaiting_parts"}).get_json()["work_state"], "awaiting_parts")
            self.assertEqual(client.post(url, json={"type": "start"}).status_code, 400)
            self.assertEqual(client.post(url, json={"type": "parts_received"}).get_json()["work_state"], "paused")
            client.post(url, json={"type": "start"})
            closed = client.post(f"/api/v1/issues/{issue['id']}/close", json={"resolution": "Done"}).get_json()

            self.assertEqual([e["type"] for e in closed["events"]],
                             ["start", "awaiting_parts", "parts_received", "start", "finish"])
            for key in ("response_minutes", "labor_minutes", "parts_wait_minutes", "resolution_minutes"):
                self.assertIsNotNone(closed["metrics"][key])

    def test_work_metrics_use_event_times(self):
        from api import work_metrics

        issue = {
            "created_at": "2026-11-11T10:00:00+00:00",
            "closed_at": "2026-11-11T14:45:00+00:00",
            "events": [
                {"type": "start", "at": "2026-11-11T10:06:00+00:00"},
                {"type": "stop", "at": "2026-11-11T10:31:00+00:00"},
                {"type": "awaiting_parts", "at": "2026-11-11T10:36:00+00:00"},
                {"type": "parts_received", "at": "2026-11-11T14:12:00+00:00"},
                {"type": "start", "at": "2026-11-11T14:18:00+00:00"},
                {"type": "finish", "at": "2026-11-11T14:45:00+00:00"},
            ],
        }

        self.assertEqual(work_metrics(issue), {
            "response_minutes": 6.0,
            "labor_minutes": 52.0,
            "parts_wait_minutes": 216.0,
            "resolution_minutes": 285.0,
        })

    def test_web_close_records_finish_event(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            store = app.config["STORE"]
            issue = store.add_issue("101", "Leak", "frontdesk", "HCK")

            store.close_issue(issue["id"], "maintenance", "Fixed", "HCK")

            self.assertEqual(store.load_issues()[0]["events"][0]["type"], "finish")

    def test_api_parts_deduct_and_return_inventory(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            client = app.test_client()
            issue = app.config["STORE"].add_issue("318", "Toilet runs", "frontdesk", "HCK", "maintenance")
            self.login(client, "maintenance")

            item = client.post("/api/v1/inventory", json={
                "name": "Flapper", "barcode": "0123456789", "qty": 3, "min_qty": 2, "unit_cost": 4.5,
            }).get_json()
            duplicate = client.post("/api/v1/inventory", json={"name": "Other", "barcode": "0123456789"})
            self.assertEqual(duplicate.status_code, 400)
            self.assertEqual(client.get("/api/v1/inventory/barcode/0123456789").get_json()["id"], item["id"])
            self.assertEqual(client.get("/api/v1/inventory/barcode/nope").status_code, 404)

            parts_url = f"/api/v1/issues/{issue['id']}/parts"
            too_many = client.post(parts_url, json={"item_id": item["id"], "qty": 5})
            self.assertEqual(too_many.status_code, 400)
            used = client.post(parts_url, json={"item_id": item["id"], "qty": 2}).get_json()
            self.assertEqual(used["parts_cost"], 9.0)
            self.assertEqual(client.get("/api/v1/inventory").get_json()["items"][0]["qty"], 1)

            returned = client.delete(f"{parts_url}/{used['parts'][0]['id']}").get_json()
            self.assertEqual(returned["parts"], [])
            self.assertEqual(client.get("/api/v1/inventory").get_json()["items"][0]["qty"], 3)

            received = client.patch(f"/api/v1/inventory/{item['id']}", json={"delta": 10}).get_json()
            self.assertEqual(received["qty"], 13)
            self.assertEqual(client.patch(f"/api/v1/inventory/{item['id']}", json={"delta": -20}).status_code, 400)

    def test_api_inventory_is_scoped_to_location_and_role(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            client = app.test_client()
            self.login(client, "maintenance")
            client.post("/api/v1/inventory", json={"name": "Filter"})
            client.post("/api/v1/logout")

            self.login(client, "frontdesk")
            self.assertEqual(client.get("/api/v1/inventory").status_code, 403)
            client.post("/api/v1/logout")

            store = app.config["STORE"]
            settings = store.load_settings()
            settings["users"][3]["location"] = "HCK2"
            store.save_settings(settings)
            self.login(client, "supervisor")
            self.assertEqual(client.get("/api/v1/inventory").get_json()["items"], [])

    def test_api_response_time_report_for_supervisors_only(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            client = app.test_client()
            store = app.config["STORE"]
            done = store.add_issue("214", "Filter", "frontdesk", "HCK", "maintenance")
            store.add_issue("305", "Remote", "frontdesk", "HCK", "guest")
            self.login(client, "maintenance")
            client.post(f"/api/v1/issues/{done['id']}/events", json={"type": "start"})
            client.post(f"/api/v1/issues/{done['id']}/close", json={"resolution": "Swapped"})
            self.assertEqual(client.get("/api/v1/reports/response-times").status_code, 403)
            client.post("/api/v1/logout")

            self.login(client, "supervisor")
            report = client.get("/api/v1/reports/response-times?days=7").get_json()

            self.assertEqual(report["days"], 7)
            self.assertEqual(report["overall"]["count"], 1)
            self.assertEqual(report["by_category"]["maintenance"]["count"], 1)
            self.assertEqual(report["by_technician"][0]["name"], "maintenance")
            self.assertEqual(report["open"]["count"], 1)
            self.assertEqual(report["open"]["not_started"], 1)


class PasswordSecurityTests(unittest.TestCase):
    """Password hashing, legacy plaintext migration, and seeded accounts."""

    def make_app(self, config_path):
        with patch.dict(os.environ, {"CONFIG_PATH": config_path, "TZ": "UTC", "TESTING": "1"}):
            os.environ.pop("SECRET_KEY", None)
            return create_app()

    def write_users(self, config_path, users):
        Path(config_path, "settings.json").write_text(json.dumps({"locations": ["HCK"], "users": users}))

    def stored_user(self, app, username):
        return next(u for u in app.config["STORE"].load_settings()["users"] if u["username"] == username)

    def test_seeded_accounts_have_random_hashed_passwords_and_must_change_them(self):
        with tempfile.TemporaryDirectory() as config_path:
            with self.assertLogs("app", level="WARNING") as logs:
                app = self.make_app(config_path)
            users = app.config["STORE"].load_settings()["users"]
            self.assertEqual({u["username"] for u in users}, {"admin", "maintenance", "frontdesk", "supervisor"})
            raw = Path(config_path, "settings.json").read_text()
            for user in users:
                self.assertTrue(user["force_password_change"])
                self.assertTrue(is_password_hash(user["password"]))
            self.assertNotIn("admin123", raw)
            self.assertNotIn("changeme", raw)

            client = app.test_client()
            for username, password in (("admin", "admin123"), ("maintenance", "changeme")):
                response = client.post("/login", data={"username": username, "password": password})
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"Invalid username or password", response.data)

            # The generated password is logged once and works, but forces a change.
            message = "\n".join(logs.output)
            admin_password = message.split("admin=", 1)[1].split(",", 1)[0]
            response = client.post("/login", data={"username": "admin", "password": admin_password})
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith("/first-login-password"))

    def test_legacy_plaintext_password_is_rehashed_on_login(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            # Written after start-up so only the login path can migrate it.
            self.write_users(config_path, [
                {"username": "legacy", "password": "old-secret", "role": "maintenance", "email": "", "location": "HCK", "force_password_change": False},
            ])
            client = app.test_client()

            response = client.post("/login", data={"username": "legacy", "password": "wrong"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.stored_user(app, "legacy")["password"], "old-secret")

            response = client.post("/login", data={"username": "Legacy", "password": "old-secret"})
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith("/dashboard"))
            stored = self.stored_user(app, "legacy")["password"]
            self.assertTrue(is_password_hash(stored))
            self.assertTrue(check_password_hash(stored, "old-secret"))
            self.assertNotIn("old-secret", Path(config_path, "settings.json").read_text())

            # The same credentials keep working against the hash.
            client.post("/logout")
            response = client.post("/login", data={"username": "legacy", "password": "old-secret"})
            self.assertEqual(response.status_code, 302)

    def test_existing_plaintext_passwords_are_hashed_at_startup(self):
        with tempfile.TemporaryDirectory() as config_path:
            self.write_users(config_path, [
                {"username": "desk", "password": "desk-pass", "role": "front_desk", "email": "", "location": "HCK"},
            ])
            app = self.make_app(config_path)
            stored = self.stored_user(app, "desk")["password"]
            self.assertTrue(check_password_hash(stored, "desk-pass"))
            response = app.test_client().post("/login", data={"username": "desk", "password": "desk-pass"})
            self.assertEqual(response.status_code, 302)

    def test_users_without_a_password_cannot_sign_in(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.write_users(config_path, [
                {"username": "nopass", "password": None, "role": "maintenance", "email": "", "location": "HCK"},
            ])
            response = app.test_client().post("/login", data={"username": "nopass", "password": ""})
            self.assertEqual(response.status_code, 200)

    def test_every_password_write_is_hashed(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.write_users(config_path, [
                {"username": "admin", "password": "admin-pass", "role": "admin", "email": "", "location": "HCK"},
                {"username": "boss", "password": "boss-pass", "role": "supervisor", "email": "", "location": "HCK"},
                {"username": "worker", "password": "worker-pass", "role": "maintenance", "email": "w@example.com", "location": "HCK"},
            ])
            client = app.test_client()

            def assert_hashed(username, password):
                stored = self.stored_user(app, username)["password"]
                self.assertTrue(is_password_hash(stored), stored)
                self.assertTrue(check_password_hash(stored, password))

            with client.session_transaction() as session:
                session["user"], session["role"] = "admin", "admin"
            client.post("/admin", data={"action": "add_user", "username": "new-admin-user", "email": "a@example.com",
                                        "password": "added-by-admin", "role": "front_desk", "location": "HCK"})
            assert_hashed("new-admin-user", "added-by-admin")
            client.post("/admin", data={"action": "change_password", "username": "worker", "new_password": "admin-set"})
            assert_hashed("worker", "admin-set")

            with client.session_transaction() as session:
                session["user"], session["role"] = "boss", "supervisor"
            client.post("/supervisor", data={"action": "add_user", "username": "new-sup-user", "email": "s@example.com",
                                             "password": "added-by-sup", "role": "front_desk"})
            assert_hashed("new-sup-user", "added-by-sup")
            client.post("/supervisor", data={"action": "change_password", "username": "worker", "new_password": "sup-set"})
            assert_hashed("worker", "sup-set")

            with client.session_transaction() as session:
                session["user"], session["role"] = "worker", "maintenance"
            client.post("/first-login-password", data={"new_password": "first-login-pw", "confirm_password": "first-login-pw"})
            assert_hashed("worker", "first-login-pw")

            settings = app.config["STORE"].load_settings()
            worker = next(u for u in settings["users"] if u["username"] == "worker")
            worker["reset_token"] = "tok"
            worker["reset_expires_at"] = "2999-01-01T00:00:00+00:00"
            app.config["STORE"].save_settings(settings)
            client.post("/reset-password/tok", data={"new_password": "reset-pw-123", "confirm_password": "reset-pw-123"})
            assert_hashed("worker", "reset-pw-123")

            raw = Path(config_path, "settings.json").read_text()
            for plaintext in ("added-by-admin", "admin-set", "added-by-sup", "sup-set", "first-login-pw", "reset-pw-123"):
                self.assertNotIn(plaintext, raw)


class SecretKeyAndCookieTests(unittest.TestCase):
    def make_app(self, config_path, **env):
        values = {"CONFIG_PATH": config_path, "TZ": "UTC"}
        values.update(env)
        with patch.dict(os.environ, values):
            if "SECRET_KEY" not in env:
                os.environ.pop("SECRET_KEY", None)
            for name in ("TESTING", "SESSION_COOKIE_SECURE", "FLASK_DEBUG"):
                if name not in env:
                    os.environ.pop(name, None)
            return create_app()

    def test_missing_secret_key_generates_and_persists_a_random_key(self):
        with tempfile.TemporaryDirectory() as config_path:
            first = self.make_app(config_path, TESTING="1")
            second = self.make_app(config_path, TESTING="1")
            key_file = Path(config_path, "secret_key")
            self.assertTrue(key_file.exists())
            self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(first.secret_key, "change-me")
            self.assertGreaterEqual(len(first.secret_key), 32)
            self.assertEqual(first.secret_key, second.secret_key)
            self.assertEqual(Store(config_path).load_or_create_secret_key(), first.secret_key)

        with tempfile.TemporaryDirectory() as other_path:
            self.assertNotEqual(self.make_app(other_path, TESTING="1").secret_key, first.secret_key)

    def test_well_known_default_secret_key_is_not_used(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path, TESTING="1", SECRET_KEY="change-me")
            self.assertNotEqual(app.secret_key, "change-me")

    def test_explicit_secret_key_is_used(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path, TESTING="1", SECRET_KEY="a-real-secret-from-the-environment")
            self.assertEqual(app.secret_key, "a-real-secret-from-the-environment")
            self.assertFalse(Path(config_path, "secret_key").exists())

    def test_forged_cookie_signed_with_old_default_is_rejected(self):
        from flask.sessions import SecureCookieSessionInterface
        from flask import Flask

        forger = Flask("forger")
        forger.secret_key = "change-me"
        serializer = SecureCookieSessionInterface().get_signing_serializer(forger)
        forged = serializer.dumps({"user": "admin", "role": "admin"})

        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path, TESTING="1")
            client = app.test_client()
            client.set_cookie("session", forged)
            response = client.get("/admin")
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith("/login"))

    def test_session_cookie_is_secure_outside_testing(self):
        with tempfile.TemporaryDirectory() as config_path:
            with self.assertLogs("app", level="WARNING"):
                app = self.make_app(config_path)
            self.assertTrue(app.config["SESSION_COOKIE_SECURE"])
            self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Lax")

    def test_session_cookie_secure_is_relaxed_for_testing_and_debug(self):
        with tempfile.TemporaryDirectory() as config_path:
            self.assertFalse(self.make_app(config_path, TESTING="1").config["SESSION_COOKIE_SECURE"])
            self.assertFalse(self.make_app(config_path, FLASK_DEBUG="1").config["SESSION_COOKIE_SECURE"])
            self.assertFalse(self.make_app(config_path, SESSION_COOKIE_SECURE="0").config["SESSION_COOKIE_SECURE"])


if __name__ == "__main__":
    unittest.main()
