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

    def test_new_requests_notify_android_phones_through_fcm(self):
        class FakeFCM:
            def __init__(self):
                self.sent = []

            def send(self, client, token, message):
                self.sent.append((token, message))
                return (404, "UNREGISTERED") if token == "gone:Token" else (200, "")

        class FakeAPNs:
            def __init__(self):
                self.sent = []

            def send(self, client, token, payload, sandbox):
                self.sent.append(token)
                return 200, ""

        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            fcm, apns = FakeFCM(), FakeAPNs()
            app.config.update(FCM_SENDER=fcm, APNS_SENDER=apns, APNS_SYNC=True)
            self.use_known_passwords(app)
            client = app.test_client()
            self.login(client, "maintenance")
            self.assertEqual(client.post("/api/v1/devices", json={"token": "x", "platform": "web"}).status_code, 400)
            self.assertEqual(client.post("/api/v1/devices", json={"token": "bad token!", "platform": "android"}).status_code, 400)
            ok = client.post("/api/v1/devices", json={"token": "fA1:APA91b-x_Y", "platform": "android"})
            self.assertTrue(ok.get_json()["notifications"])
            client.post("/api/v1/devices", json={"token": "ab12"})  # an iPhone, no platform given
            from apns import Devices
            Devices(config_path).register("gone:Token", "maintenance", "maintenance", "HCK", False, "android")

            client.post("/api/v1/issues", json={"room": "214", "issues": [{"description": "Leak", "category": "guest"}]})

            self.assertEqual(sorted(t for t, _ in fcm.sent), ["fA1:APA91b-x_Y", "gone:Token"])  # case kept
            self.assertEqual(apns.sent, ["ab12"])
            message = fcm.sent[0][1]
            self.assertEqual(message["notification"]["title"], "New job · Room 214")
            self.assertEqual(message["android"]["notification"]["channel_id"], "new_jobs")
            self.assertEqual(message["data"]["room"], "214")
            tokens = [d["token"] for d in Devices(config_path).load()]
            self.assertNotIn("gone:Token", tokens)

            client.delete("/api/v1/devices/fA1:APA91b-x_Y")
            self.assertEqual([d["token"] for d in Devices(config_path).load()], ["ab12"])

    def test_fcm_sender_signs_assertion_and_reads_error_codes(self):
        import httpx
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from fcm import FCMSender

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        sender = FCMSender({"project_id": "hc-test", "client_email": "push@hc-test.iam.gserviceaccount.com",
                            "private_key": pem})
        claims = jwt.decode(sender.assertion(), key.public_key(), algorithms=["RS256"],
                            audience="https://oauth2.googleapis.com/token")
        self.assertEqual(claims["scope"], "https://www.googleapis.com/auth/firebase.messaging")

        calls = []

        def handler(request):
            calls.append(str(request.url))
            if "oauth2" in str(request.url):
                return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600})
            if request.headers["authorization"] != "Bearer ya29.test":
                return httpx.Response(401)
            body = json.loads(request.content)
            if body["message"]["token"] == "old":
                return httpx.Response(404, json={"error": {"status": "NOT_FOUND", "details": [
                    {"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": "UNREGISTERED"}]}})
            return httpx.Response(200, json={"name": "projects/hc-test/messages/1"})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(sender.send(client, "good", {"notification": {"title": "t"}}), (200, ""))
            self.assertEqual(sender.send(client, "old", {}), (404, "UNREGISTERED"))
        self.assertEqual(sum("oauth2" in c for c in calls), 1)  # access token reused
        self.assertIn("/v1/projects/hc-test/messages:send", calls[1])

        with patch.dict(os.environ, {"FCM_SERVICE_ACCOUNT": "not json"}):
            self.assertIsNone(FCMSender.from_env())

    def test_emergency_alert_reaches_every_phone_and_can_be_answered_and_resolved(self):
        class FakeFCM:
            def __init__(self):
                self.sent = []

            def send(self, client, token, message):
                self.sent.append((token, message))
                return 200, ""

        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            fcm = FakeFCM()
            app.config.update(FCM_SENDER=fcm, APNS_SENDER=None, APNS_SYNC=True)
            self.use_known_passwords(app)
            from apns import Devices
            reg = Devices(config_path).register
            reg("fd:1", "frontdesk", "front_desk", "HCK", False, "android")      # front desk does get emergencies
            reg("mt:1", "maintenance", "maintenance", "HCK", False, "android")   # the person raising it
            reg("sv:1", "boss", "supervisor", "Elsewhere", False, "android")     # supervisor on call off-site
            reg("ot:1", "other", "maintenance", "Elsewhere", False, "android")   # other property: no

            tech = app.test_client()
            self.login(tech, "maintenance")
            self.assertEqual(tech.post("/api/v1/emergency", json={"trigger": "nope"}).status_code, 400)
            raised = tech.post("/api/v1/emergency", json={"near": "Room 214"})
            self.assertEqual(raised.status_code, 201)
            alert = raised.get_json()
            self.assertEqual(sorted(t for t, _ in fcm.sent), ["fd:1", "sv:1"])
            data = fcm.sent[0][1]["data"]
            self.assertEqual((data["type"], data["emergency"], data["near"]), ("emergency", "raised", "Room 214"))
            self.assertNotIn("notification", fcm.sent[0][1])  # data-only: the app shows the alarm

            # Pressing again doesn't open a second alert.
            again = tech.post("/api/v1/emergency", json={"near": "Room 216"})
            self.assertEqual((again.status_code, again.get_json()["id"], again.get_json()["near"]), (200, alert["id"], "Room 216"))

            desk = app.test_client()
            self.login(desk, "frontdesk")
            self.assertEqual([a["id"] for a in desk.get("/api/v1/emergency").get_json()["active"]], [alert["id"]])
            responded = desk.post(f"/api/v1/emergency/{alert['id']}/respond").get_json()
            self.assertEqual([r["username"] for r in responded["responders"]], ["frontdesk"])
            self.assertEqual(desk.post(f"/api/v1/emergency/{alert['id']}/resolve").status_code, 403)

            fcm.sent.clear()
            done = tech.post(f"/api/v1/emergency/{alert['id']}/resolve", json={"note": "False alarm"}).get_json()
            self.assertEqual((done["status"], done["resolved_by"], done["note"]), ("resolved", "maintenance", "False alarm"))
            self.assertTrue(all(m["data"]["emergency"] == "resolved" for _, m in fcm.sent))
            self.assertEqual(desk.get("/api/v1/emergency").get_json()["active"], [])

            # A test alert notifies nobody.
            fcm.sent.clear()
            tech.post("/api/v1/emergency", json={"trigger": "test"})
            self.assertEqual(fcm.sent, [])

            # Usage record for L&I: every alert, drills included; supervisors only.
            self.assertEqual(desk.get("/api/v1/emergency/log.csv").status_code, 403)
            sup = app.test_client()
            self.login(sup, "supervisor")
            log = sup.get("/api/v1/emergency/log.csv").get_data(as_text=True).splitlines()
            self.assertEqual(len(log), 3)  # header + real alert + drill
            self.assertIn("False alarm", log[1])
            self.assertIn("frontdesk", log[1])

    def test_enrolled_kiosk_phone_raises_sos_with_nobody_signed_in(self):
        class FakeFCM:
            def __init__(self):
                self.sent = []

            def send(self, client, token, message):
                self.sent.append((token, message))
                return 200, ""

        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            fcm = FakeFCM()
            app.config.update(FCM_SENDER=fcm, APNS_SENDER=None, APNS_SYNC=True)
            self.use_known_passwords(app)
            from apns import Devices
            Devices(config_path).register("sv:1", "boss", "supervisor", "Elsewhere", False, "android")

            sup = app.test_client()
            self.login(sup, "supervisor")
            desk = app.test_client()
            self.login(desk, "frontdesk")

            # Only supervisors make enrollment codes; the code is shown once and stored hashed.
            self.assertEqual(desk.post("/api/v1/kiosk/codes", json={}).status_code, 403)
            made = sup.post("/api/v1/kiosk/codes", json={"label": "XP8 batch"})
            self.assertEqual(made.status_code, 201)
            code = made.get_json()["code"]
            self.assertTrue(code.startswith("hce_"))
            self.assertNotIn(code, open(os.path.join(config_path, "kiosk_devices.json")).read())
            self.assertNotIn("code", sup.get("/api/v1/kiosk/devices").get_json()["codes"][0])

            # A phone in setup trades the code for its own key (no session).
            phone = app.test_client()
            self.assertEqual(phone.post("/api/v1/kiosk/enroll", json={"code": "hce_wrong"}).status_code, 403)
            enrolled = phone.post("/api/v1/kiosk/enroll", json={"code": code, "name": "Housekeeping 2", "serial": "XP8-1234"}).get_json()
            key = enrolled["device_key"]
            self.assertEqual((enrolled["name"], enrolled["location"]), ("Housekeeping 2", "HCK"))
            auth = {"Authorization": f"Bearer {key}"}
            self.assertEqual(phone.get("/api/v1/kiosk/device", headers=auth).get_json()["name"], "Housekeeping 2")

            # With nobody signed in: no key, no alert; with the key, an alert named for the phone.
            self.assertEqual(phone.post("/api/v1/emergency", json={}).status_code, 401)
            self.assertEqual(phone.post("/api/v1/emergency", json={}, headers={"Authorization": "Bearer hck_nope"}).status_code, 401)
            raised = phone.post("/api/v1/emergency", json={"near": "Room 214"}, headers=auth)
            self.assertEqual(raised.status_code, 201)
            alert = raised.get_json()
            self.assertEqual(alert["raised_by"], "Housekeeping 2")
            self.assertEqual([t for t, _ in fcm.sent], ["sv:1"])
            self.assertEqual(fcm.sent[0][1]["data"]["raised_by"], "Housekeeping 2")
            # The key can't do anything else.
            self.assertEqual(phone.get("/api/v1/issues", headers=auth).status_code, 401)
            self.assertEqual(phone.get("/api/v1/beacons/map", headers=auth).status_code, 200)

            # Only a supervisor can resolve it; the log names the phone.
            self.assertEqual(desk.post(f"/api/v1/emergency/{alert['id']}/resolve").status_code, 403)
            self.assertEqual(sup.post(f"/api/v1/emergency/{alert['id']}/resolve").status_code, 200)
            log = sup.get("/api/v1/emergency/log.csv").get_data(as_text=True).splitlines()
            self.assertTrue(log[0].endswith(",device"))
            self.assertTrue(log[1].endswith(",Housekeeping 2"))

            # A signed-in worker's alert is theirs, with the phone noted.
            tech = app.test_client()
            self.login(tech, "maintenance")
            mine = tech.post("/api/v1/emergency", json={"trigger": "test"}, headers=auth).get_json()
            self.assertEqual(mine["raised_by"], "maintenance")

            # Supervisor signed in on a USB-set-up phone enrolls it directly.
            self.assertEqual(desk.post("/api/v1/kiosk/devices", json={"name": "Desk"}).status_code, 403)
            self.assertEqual(sup.post("/api/v1/kiosk/devices", json={}).status_code, 400)
            direct = sup.post("/api/v1/kiosk/devices", json={"name": "Engineering 1"}).get_json()
            self.assertTrue(direct["device_key"].startswith("hck_"))
            listing = sup.get("/api/v1/kiosk/devices").get_json()
            self.assertEqual([d["name"] for d in listing["devices"]], ["Housekeeping 2", "Engineering 1"])
            self.assertIsNotNone(listing["devices"][0]["last_seen_at"])
            self.assertNotIn("key_hash", listing["devices"][0])

            # Revoking a lost phone stops its key; revoking a code stops new enrollments.
            device_id = enrolled["device_id"]
            self.assertTrue(sup.patch(f"/api/v1/kiosk/devices/{device_id}", json={"revoked": True}).get_json()["revoked_at"])
            self.assertEqual(phone.post("/api/v1/emergency", json={}, headers=auth).status_code, 401)
            code_id = made.get_json()["id"]
            self.assertEqual(sup.delete(f"/api/v1/kiosk/codes/{code_id}").status_code, 200)
            self.assertEqual(phone.post("/api/v1/kiosk/enroll", json={"code": code}).status_code, 403)

    def test_kiosk_phone_identifiers_are_for_supervisors_and_admins_only(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            sup = app.test_client()
            self.login(sup, "supervisor")
            code = sup.post("/api/v1/kiosk/codes", json={}).get_json()["code"]
            phone = app.test_client()
            info = {"imei": "35-209900-176148-1", "iccid": "", "serial": "XP8-1234", "model": "XP8800",
                    "android": "8.1.0", "app_version": "1.0 (7)", "battery": 87}
            key = phone.post("/api/v1/kiosk/enroll", json={"code": code, "name": "Engineering 1", "info": info}
                             ).get_json()["device_key"]
            auth = {"Authorization": f"Bearer {key}"}

            device = sup.get("/api/v1/kiosk/devices").get_json()["devices"][0]
            self.assertEqual((device["imei"], device["iccid"], device["serial"]), ("352099001761481", None, "XP8-1234"))
            self.assertEqual((device["android"], device["app_version"], device["battery"]), ("8.1.0", "1.0 (7)", 87))

            # The SIM gets activated later: the next check-in fills in the ICCID and keeps the rest.
            self.assertEqual(phone.post("/api/v1/kiosk/checkin", json={"info": {"iccid": "8901410321111851072F",
                                                                                "battery": 140}}).status_code, 401)
            self.assertEqual(phone.post("/api/v1/kiosk/checkin", headers=auth, json={"info": {
                "iccid": "8901410321111851072F", "battery": 140, "imei": "not a number"}}).status_code, 200)
            device = sup.get("/api/v1/kiosk/devices").get_json()["devices"][0]
            self.assertEqual((device["iccid"], device["imei"], device["battery"]), ("8901410321111851072F", "352099001761481", 87))
            self.assertIsNotNone(device["checked_in_at"])

            # Nobody else sees them: other roles can't list phones, and the phone's own view leaves them out.
            for role in ("maintenance", "frontdesk"):
                other = app.test_client()
                self.login(other, role)
                self.assertEqual(other.get("/api/v1/kiosk/devices").status_code, 403)
            mine = phone.get("/api/v1/kiosk/device", headers=auth).get_json()
            self.assertFalse({"imei", "iccid", "serial"} & mine.keys())
            page = sup.get("/supervisor").get_data(as_text=True)
            self.assertIn("352099001761481", page)
            self.assertIn("8901410321111851072F", page)

    def test_room_beacons_need_beacon_access_and_report_status(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            sup = app.test_client()
            self.login(sup, "supervisor")
            sup.put("/api/v1/property", json={"rooms": ["101", "214"], "areas": ["Lobby"]})
            tech = app.test_client()
            self.login(tech, "maintenance")

            self.assertFalse(tech.get("/api/v1/me").get_json()["beacon_access"])
            self.assertEqual(tech.get("/api/v1/beacons").status_code, 403)
            self.assertEqual(tech.put("/api/v1/beacons/ABC", json={"place": "214"}).status_code, 403)
            self.assertTrue(sup.get("/api/v1/me").get_json()["beacon_access"])
            self.assertEqual(sup.patch("/api/v1/users/maintenance", json={"beacon_access": "yes"}).status_code, 400)
            granted = sup.patch("/api/v1/users/maintenance", json={"beacon_access": True}).get_json()
            self.assertTrue(granted["beacon_access"])

            uuid_id = "f7826da6-4fa2-4e98-8024-bc5b71e0893e:1:214"
            self.assertEqual(tech.put("/api/v1/beacons/bad id!", json={"place": "214"}).status_code, 400)
            self.assertEqual(tech.put(f"/api/v1/beacons/{uuid_id}", json={"place": "999"}).status_code, 400)
            made = tech.put(f"/api/v1/beacons/{uuid_id}", json={"place": "room 214", "label": "behind TV"}).get_json()
            self.assertEqual((made["id"], made["place"], made["status"]), (uuid_id.upper(), "214", "never"))
            clash = tech.put("/api/v1/beacons/AA:BB:CC:DD:EE:FF", json={"place": "214"})
            self.assertEqual(clash.status_code, 400)  # one beacon per room
            tech.put("/api/v1/beacons/AA:BB:CC:DD:EE:FF", json={"place": "Lobby"})

            listing = tech.get("/api/v1/beacons").get_json()
            self.assertEqual([b["place"] for b in listing["beacons"]], ["214", "LOBBY"])
            self.assertEqual(listing["rooms_without_beacon"], ["101"])

            # Any phone (front desk included) reports what it hears; unknown beacons are ignored.
            desk = app.test_client()
            self.login(desk, "frontdesk")
            heard = desk.post("/api/v1/beacons/sightings", json={"sightings": [
                {"id": uuid_id.lower(), "rssi": -61, "battery": 15}, {"id": "11:22", "rssi": -80}]}).get_json()
            self.assertEqual(heard["matched"], 1)
            b214 = next(b for b in tech.get("/api/v1/beacons").get_json()["beacons"] if b["place"] == "214")
            self.assertEqual((b214["status"], b214["battery"], b214["last_seen_by"], b214["rssi"]),
                             ("low_battery", 15, "frontdesk", -61))
            self.assertEqual(len(desk.get("/api/v1/beacons/map").get_json()["beacons"]), 2)

            # The emergency alert names the room from the beacon the phone heard.
            alert = desk.post("/api/v1/emergency", json={"trigger": "test", "beacon_id": uuid_id}).get_json()
            self.assertEqual(alert["near"], "Room 214")

            self.assertEqual(tech.delete("/api/v1/beacons/AA:BB:CC:DD:EE:FF").status_code, 200)
            self.assertEqual(tech.delete("/api/v1/beacons/AA:BB:CC:DD:EE:FF").status_code, 404)

    def test_beacon_status_goes_missing_after_a_day(self):
        from datetime import timedelta
        from beacons import beacon_status
        now = datetime.now(timezone.utc)
        self.assertEqual(beacon_status({"last_seen_at": (now - timedelta(hours=2)).isoformat(), "battery": 80}, now), "ok")
        self.assertEqual(beacon_status({"last_seen_at": (now - timedelta(hours=30)).isoformat()}, now), "missing")

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
                headboard = next(i for i in quarter["items"] if i["name"] == "Headboard")
                self.assertEqual(headboard["options"], ["ok", "clean", "repair", "replace"])
                self.assertNotIn("Drapes: Clean/Charged", [i["name"] for i in quarter["items"]])
                rooms = {r["room"]: r for r in quarter["rooms"]}
                # Only listed rooms get PM; 305 isn't on the list, so its request is listed separately.
                self.assertEqual(list(rooms), ["115", "119", "210"])
                self.assertEqual(rooms["119"]["open_requests"], 1)
                self.assertEqual([i["room"] for i in quarter["other_requests"]], ["305"])
                self.assertEqual(client.get("/api/v1/pm?year=2027&quarter=1").status_code, 400)

                partial = client.put("/api/v1/pm/rooms/119/2026/4", json={"ratings": [{"item": "Mirror", "rating": "ok"}]})
                self.assertEqual(partial.status_code, 200)
                self.assertEqual(client.put("/api/v1/pm/rooms/119/2026/4", json={
                    "ratings": {"Mirror": "ok"}, "complete": True}).status_code, 400)
                # Each item offers only its own options: walls can be painted, a headboard can't.
                self.assertEqual(client.put("/api/v1/pm/rooms/119/2026/4", json={
                    "ratings": {"Headboard": "paint"}}).status_code, 400)
                ratings = {i["name"]: "ok" for i in DEFAULT_PM_ITEMS}
                ratings["Switches / Cover Plates"] = "replace"
                ratings["Walls"] = "paint"
                done = client.put("/api/v1/pm/rooms/119/2026/4", json={
                    "ratings": ratings, "notes": "Replaced switch plate", "complete": True,
                    "smoke_detector_date": "2019-06-01",
                    "extra_tag_dates": ["2026-11-20"],
                    "close_request_ids": [pm_request["id"]],
                }).get_json()
                self.assertEqual(done["quarters"]["4"]["date"], "2026-11-13")
                self.assertIn({"item": "Switches / Cover Plates", "rating": "replace"}, done["quarters"]["4"]["ratings"])
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
                # Q4 recorded the detector date; it carries over to the room.
                self.assertEqual(detail["smoke_detector_date"], "2019-06-01")
                self.assertEqual(detail["quarters"]["4"]["extra_tag_dates"], ["2026-11-20"])
                self.assertEqual(client.put("/api/v1/pm/rooms/119/2026/4", json={
                    "ratings": [], "smoke_detector_date": "June"}).status_code, 400)

    def test_requests_must_name_a_listed_room_or_area_once_the_list_exists(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            client = app.test_client()
            self.login(client, "supervisor")

            def create(room):
                return client.post("/api/v1/issues", json={"room": room, "issues": [{"description": "x", "category": "guest"}]})

            # No list yet: anything goes, as before.
            legacy = create("light outside 223")
            self.assertEqual(legacy.status_code, 201)
            self.assertEqual(client.put("/api/v1/property", json={"areas": ["223"], "rooms": ["223"]}).status_code, 400)
            saved = client.put("/api/v1/property", json={
                "rooms": ["223", "224"], "areas": ["Lobby", "pool", " Lobby ", "Exterior"],
            }).get_json()
            self.assertEqual(saved["areas"], ["Exterior", "Lobby", "pool"])

            self.assertEqual(create("Room 223").get_json()["issues"][0]["room"], "223")
            self.assertEqual(create("lobby").get_json()["issues"][0]["room"], "LOBBY")
            refused = create("light outside 223")
            self.assertEqual(refused.status_code, 400)
            self.assertIn("isn't a room or area", refused.get_json()["error"])
            self.assertEqual(create("2233").status_code, 400)

            # An older request keeps its room when edited, or can be moved to a real place.
            issue_id = legacy.get_json()["issues"][0]["id"]
            kept = client.patch(f"/api/v1/issues/{issue_id}", json={
                "room": "LIGHT OUTSIDE 223", "description": "Light out", "category": "guest"})
            self.assertEqual(kept.status_code, 200)
            moved = client.patch(f"/api/v1/issues/{issue_id}", json={
                "room": "exterior", "description": "Light outside 223", "category": "guest"})
            self.assertEqual(moved.get_json()["room"], "EXTERIOR")

            # The web form follows the same rules and suggests the places.
            with client.session_transaction() as session:
                session["user"] = "supervisor"
                session["role"] = "supervisor"
            client.post("/issues", data={"room": "nowhere", "row": "1", "description_1": "x", "category_1": "guest"})
            self.assertFalse(any(i["room"] == "NOWHERE" for i in app.config["STORE"].load_issues()))
            page = client.get("/dashboard").data
            self.assertIn(b'<option value="Lobby">', page)

            client.post("/supervisor", data={"action": "save_places", "rooms": "101-103, 223", "areas": "Lobby\nPool\n"})
            saved = client.get("/api/v1/property").get_json()
            self.assertEqual((saved["rooms"], saved["areas"]), (["101", "102", "103", "223"], ["Lobby", "Pool"]))
            self.assertIn(b"101, 102, 103, 223", client.get("/supervisor").data)

    def test_room_type_codes(self):
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            client = app.test_client()
            self.login(client, "supervisor")
            client.put("/api/v1/property", json={"rooms": ["115", "116", "117", "119", "210", "305B"]})

            saved = client.put("/api/v1/property", json={"room_types": [
                {"room": "115", "type": "kxtd"}, {"room": "305b", "type": "SXBV"}]}).get_json()
            self.assertIn({"room": "115", "type": "KXTD"}, saved["room_types"])
            self.assertIn({"room": "305B", "type": "SXBV"}, saved["room_types"])
            self.assertEqual(client.put("/api/v1/property", json={"room_types": [{"room": "999", "type": "X"}]}).status_code, 400)
            cleared = client.put("/api/v1/property", json={"room_types": [{"room": "305B", "type": ""}]}).get_json()
            self.assertEqual(cleared["room_types"], [{"room": "115", "type": "KXTD"}])
            # Taking a room off the list drops its type.
            self.assertEqual(client.put("/api/v1/property", json={"rooms": ["116"]}).get_json()["room_types"], [])

            # Website form: ranges in, ranges back out.
            with client.session_transaction() as session:
                session["user"] = "supervisor"
                session["role"] = "supervisor"
            client.post("/supervisor", data={"action": "save_places", "rooms": "115-119, 210", "areas": "Lobby",
                                             "room_types": "115-118 KXTD\n119 SXBV\n210 NQRUB"})
            page = client.get("/supervisor").data
            self.assertIn(b"115-118 KXTD\n119 SXBV\n210 NQRUB", page)
            self.assertEqual(client.post("/supervisor", data={"action": "save_places", "rooms": "115", "areas": "",
                                                              "room_types": "KXTD"}).status_code, 302)
            with patch("api.datetime") as fake_datetime:
                fake_datetime.now.return_value = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
                rooms = {r["room"]: r for r in client.get("/api/v1/pm").get_json()["rooms"]}
                self.assertEqual((rooms["119"]["type"], rooms["210"]["type"]), ("SXBV", "NQRUB"))
                self.assertEqual(client.get("/api/v1/pm/rooms/210").get_json()["room_type"], "NQRUB")

    def test_pm_checklists_saved_before_item_options_still_read(self):
        from pm import PMRecords, clean_items
        with tempfile.TemporaryDirectory() as config_path:
            app = self.make_app(config_path)
            self.use_known_passwords(app)
            # The first version stored long names and 0-3 numbers (0 Paint, 1 Repair, 2 Replace, 3 OK).
            PMRecords(config_path).upsert("HCK", "115", 2026, 1, {
                "ratings": {"Walls: Free of marks/clean": 0, "Drapes: Clean/Charged": 3}, "completed": False})
            client = app.test_client()
            self.login(client, "maintenance")
            q1 = client.get("/api/v1/pm/rooms/115?year=2026").get_json()["quarters"]["1"]["ratings"]
            self.assertIn({"item": "Walls", "rating": "paint"}, q1)
            self.assertIn({"item": "Drapes", "rating": "ok"}, q1)

        items, message = clean_items(["Ceiling", {"name": "Minibar", "options": ["clean", "replace"]}])
        self.assertIsNone(message)
        self.assertEqual(items[1], {"name": "Minibar", "options": ["ok", "clean", "replace"]})
        self.assertIsNotNone(clean_items([{"name": "Minibar", "options": ["ok"]}])[1])
        self.assertIsNotNone(clean_items([{"name": "Minibar", "options": ["charge"]}])[1])

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
