import csv
import json
import os
import uuid
from io import StringIO
from base64 import urlsafe_b64encode
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock
from urllib import error, request as urllib_request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from flask import Flask, Response, flash, jsonify, redirect, render_template, request, session, url_for
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

DEFAULT_LOCATION = "HCK"

DEFAULT_SETTINGS = {
    "locations": [DEFAULT_LOCATION],
    "users": [
        {"username": "admin", "password": "admin123", "role": "admin", "email": "", "location": DEFAULT_LOCATION, "force_password_change": False},
        {
            "username": "maintenance",
            "password": "changeme",
            "role": "maintenance",
            "email": "",
            "location": DEFAULT_LOCATION,
            "force_password_change": False,
        },
        {"username": "frontdesk", "password": "changeme", "role": "front_desk", "email": "", "location": DEFAULT_LOCATION, "force_password_change": False},
        {"username": "supervisor", "password": "changeme", "role": "supervisor", "email": "", "location": DEFAULT_LOCATION, "force_password_change": False},
    ]
}

_vapid_key_creation_lock = Lock()


class Store:
    def __init__(self, config_dir: str):
        self.config_dir = Path(config_dir)
        self.settings_file = self.config_dir / "settings.json"
        self.issues_file = self.config_dir / "issues.json"
        self.push_subscriptions_file = self.config_dir / "push_subscriptions.json"
        self.vapid_private_key_file = self.config_dir / "vapid_private.pem"
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def _write(self, path: Path, data) -> None:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def load_settings(self) -> dict:
        if not self.settings_file.exists():
            data = deepcopy(DEFAULT_SETTINGS)
            self._write(self.settings_file, data)
            return data
        try:
            with self.settings_file.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return deepcopy(DEFAULT_SETTINGS)

    def save_settings(self, data: dict) -> None:
        self._write(self.settings_file, data)

    def load_issues(self) -> list:
        if not self.issues_file.exists():
            return []
        try:
            with self.issues_file.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return []

    def save_issues(self, issues: list) -> None:
        self._write(self.issues_file, issues)

    def load_push_subscriptions(self) -> list:
        if not self.push_subscriptions_file.exists():
            return []
        try:
            with self.push_subscriptions_file.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return []

    def save_push_subscription(self, subscription: dict, username: str, role: str, location: str) -> None:
        subscriptions = self.load_push_subscriptions()
        endpoint = subscription["endpoint"]
        subscriptions = [item for item in subscriptions if item.get("subscription", {}).get("endpoint") != endpoint]
        subscriptions.append({"username": username, "role": role, "location": location, "subscription": subscription})
        self._write(self.push_subscriptions_file, subscriptions)

    def remove_push_subscription(self, endpoint: str) -> None:
        subscriptions = self.load_push_subscriptions()
        remaining = [item for item in subscriptions if item.get("subscription", {}).get("endpoint") != endpoint]
        if len(remaining) != len(subscriptions):
            self._write(self.push_subscriptions_file, remaining)

    def load_or_create_vapid_keys(self) -> tuple[Vapid, str]:
        """Return a persistent VAPID key pair for push notifications."""
        vapid = Vapid.from_file(str(self.vapid_private_key_file))
        self.vapid_private_key_file.chmod(0o600)
        public_key = vapid.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        return vapid, urlsafe_b64encode(public_key).rstrip(b"=").decode("ascii")

    def add_issue(self, room: str, description: str, created_by: str, location: str) -> dict:
        issues = self.load_issues()
        issue = {
            "id": str(uuid.uuid4()),
            "room": room.strip().upper(),
            "description": description.strip(),
            "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": created_by,
            "location": location,
            "closed_at": None,
            "closed_by": None,
            "resolution": None,
        }
        issues.append(issue)
        self.save_issues(issues)
        return issue

    def close_issue(self, issue_id: str, closed_by: str, resolution: str, location: str | None = None) -> bool:
        issues = self.load_issues()
        for issue in issues:
            if (
                issue["id"] == issue_id
                and issue["status"] == "open"
                and (location is None or issue.get("location", DEFAULT_LOCATION) == location)
            ):
                issue["status"] = "closed"
                issue["closed_at"] = datetime.now(timezone.utc).isoformat()
                issue["closed_by"] = closed_by
                issue["resolution"] = resolution.strip()
                self.save_issues(issues)
                return True
        return False


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.getenv("SECRET_KEY", "change-me")

    store = Store(os.getenv("CONFIG_PATH", "/data"))
    app.config["STORE"] = store
    app.config["RESEND_API_KEY"] = os.getenv("RESEND_API_KEY", "")
    app.config["RESEND_FROM"] = os.getenv("RESEND_FROM", "noreply@bmiMaintenance.com")
    vapid_public_key = os.getenv("VAPID_PUBLIC_KEY", "").strip()
    vapid_private_key = os.getenv("VAPID_PRIVATE_KEY", "").strip()
    if not (vapid_public_key and vapid_private_key):
        if vapid_public_key or vapid_private_key:
            app.logger.warning("Ignoring incomplete VAPID environment configuration and using the persisted key pair.")
        vapid_private_key, vapid_public_key = store.load_or_create_vapid_keys()
    app.config["VAPID_PUBLIC_KEY"] = vapid_public_key
    app.config["VAPID_PRIVATE_KEY"] = vapid_private_key
    app.config["VAPID_CLAIMS_EMAIL"] = os.getenv("VAPID_CLAIMS_EMAIL", "mailto:admin@example.com").strip()

    timezone_name = os.getenv("TZ", "UTC").strip() or "UTC"
    try:
        display_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        app.logger.warning("Unknown TZ %r; displaying dates in UTC.", timezone_name)
        display_timezone = timezone.utc

    @app.template_filter("fmtdate")
    def fmt_date(value):
        if not value:
            return "—"
        try:
            dt = datetime.fromisoformat(str(value))
            # Stored issue timestamps are UTC. Treat timestamps from older data
            # that lack an offset as UTC before converting them for display.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(display_timezone)
            return dt.strftime("%b %d %Y %H:%M")
        except (ValueError, AttributeError):
            return str(value)[:10]

    def _require(roles=None):
        if not session.get("user"):
            return redirect(url_for("login"))
        if roles and session.get("role") not in roles:
            flash("Access denied.", "error")
            return redirect(url_for("dashboard"))
        return None

    def _normalize_users(settings: dict) -> bool:
        """Add location configuration and fields required by older settings files."""
        changed = False
        locations = []
        for location in settings.get("locations", []):
            cleaned_location = str(location).strip()
            if cleaned_location and cleaned_location.casefold() not in {item.casefold() for item in locations}:
                locations.append(cleaned_location)
        if not locations:
            locations.append(DEFAULT_LOCATION)
        if DEFAULT_LOCATION.casefold() not in {item.casefold() for item in locations}:
            locations.insert(0, DEFAULT_LOCATION)

        for user in settings.get("users", []):
            if "email" not in user:
                user["email"] = ""
                changed = True
            if not user.get("location"):
                user["location"] = DEFAULT_LOCATION
                changed = True
            elif user["location"] == "Default Hotel":
                # Migrate the original default assignment to the new HCK default.
                user["location"] = DEFAULT_LOCATION
                changed = True
            if user["location"].casefold() not in {item.casefold() for item in locations}:
                locations.append(user["location"])
                changed = True
            if "force_password_change" not in user:
                user["force_password_change"] = False
                changed = True
            if "reset_token" not in user:
                user["reset_token"] = None
                changed = True
            if "reset_expires_at" not in user:
                user["reset_expires_at"] = None
                changed = True
        if settings.get("locations") != locations:
            settings["locations"] = locations
            changed = True
        return changed

    def _current_user() -> dict | None:
        """Load the signed-in user's current assignment from persisted settings."""
        username = session.get("user")
        if not username:
            return None
        settings = store.load_settings()
        if _normalize_users(settings):
            store.save_settings(settings)
        return next((user for user in settings["users"] if user["username"] == username), None)

    def _location_for_current_user() -> str | None:
        user = _current_user()
        return user.get("location") if user else None

    def _send_email_via_resend(to_email: str, subject: str, html: str) -> tuple[bool, str]:
        api_key = app.config["RESEND_API_KEY"].strip()
        if not api_key:
            return False, "RESEND_API_KEY is not configured."
        from_email = app.config["RESEND_FROM"].strip()
        payload = {
            "from": from_email,
            "to": [to_email],
            "subject": subject,
            "html": html,
            "text": "Use the password reset link in the HTML email body.",
        }
        try:
            response = requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "maintenance-tracker/1.0",
                },
                json=payload,
                timeout=10,
            )
            if 200 <= response.status_code < 300:
                return True, ""

            response_body = response.text.strip()
            app.logger.error(
                "Resend email failed (status=%s, from=%s, to=%s, body=%s)",
                response.status_code,
                from_email,
                to_email,
                response_body,
            )
            return False, f"Email API rejected request ({response.status_code})."
        except requests.RequestException as exc:
            app.logger.exception("Resend email request failed: %s", exc)
            return False, "Unable to reach email API."

    def _send_new_issue_notifications(issue: dict) -> None:
        private_key = app.config["VAPID_PRIVATE_KEY"]
        if not private_key:
            app.logger.warning("New-job push notification skipped: VAPID_PRIVATE_KEY is not configured.")
            return

        payload = json.dumps(
            {
                "title": f"New maintenance job · Room {issue['room']}",
                "body": issue["description"],
                "url": url_for("dashboard"),
                "tag": f"issue-{issue['id']}",
            }
        )
        for item in store.load_push_subscriptions():
            if item.get("role") not in ("maintenance", "admin"):
                continue
            if item.get("location", DEFAULT_LOCATION) != issue["location"]:
                continue
            subscription = item.get("subscription", {})
            try:
                webpush(
                    subscription_info=subscription,
                    data=payload,
                    vapid_private_key=private_key,
                    vapid_claims={"sub": app.config["VAPID_CLAIMS_EMAIL"]},
                    timeout=10,
                    ttl=86400,
                )
            except WebPushException as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code in (404, 410):
                    store.remove_push_subscription(subscription.get("endpoint", ""))
                else:
                    app.logger.warning("Unable to send new-job push notification: %s", exc)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("user"):
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "").strip()
            settings = store.load_settings()
            user = next(
                (u for u in settings["users"] if u["username"].lower() == username and u["password"] == password),
                None,
            )
            if user:
                session["user"] = user["username"]
                session["role"] = user["role"]
                if user.get("force_password_change"):
                    return redirect(url_for("first_login_password_change"))
                return redirect(url_for("dashboard"))
            flash("Invalid username or password.", "error")
        return render_template("login.html")

    @app.route("/first-login-password", methods=["GET", "POST"])
    def first_login_password_change():
        if not session.get("user"):
            return redirect(url_for("login"))
        settings = store.load_settings()
        if _normalize_users(settings):
            store.save_settings(settings)
        user = next((u for u in settings["users"] if u["username"] == session["user"]), None)
        if not user:
            session.clear()
            return redirect(url_for("login"))
        if request.method == "POST":
            new_password = request.form.get("new_password", "").strip()
            confirm_password = request.form.get("confirm_password", "").strip()
            if len(new_password) < 8:
                flash("Password must be at least 8 characters.", "error")
            elif new_password != confirm_password:
                flash("Passwords do not match.", "error")
            else:
                user["password"] = new_password
                user["force_password_change"] = False
                store.save_settings(settings)
                flash("Password updated successfully.", "success")
                return redirect(url_for("dashboard"))
        return render_template("first_login_password.html")

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        if request.method == "POST":
            username = request.form.get("username", "").strip().lower()
            settings = store.load_settings()
            if _normalize_users(settings):
                store.save_settings(settings)
            user = next((u for u in settings["users"] if u["username"].lower() == username), None)
            if not user or not user.get("email"):
                flash("If the account exists, reset instructions were sent.", "success")
                return redirect(url_for("forgot_password"))

            token = uuid.uuid4().hex
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
            user["reset_token"] = token
            user["reset_expires_at"] = expires_at
            store.save_settings(settings)
            reset_link = url_for("reset_password", token=token, _external=True)
            ok, error_text = _send_email_via_resend(
                user["email"],
                "Reset your BMI Maintenance password",
                (
                    f"<p>Hello {user['username']},</p>"
                    f"<p>Use this link to reset your password (valid for 30 minutes):</p>"
                    f"<p><a href='{reset_link}'>{reset_link}</a></p>"
                ),
            )
            if ok:
                flash("If the account exists, reset instructions were sent.", "success")
            else:
                flash(f"Unable to send reset email right now. {error_text}", "error")
            return redirect(url_for("forgot_password"))
        return render_template("forgot_password.html")

    @app.route("/reset-password/<token>", methods=["GET", "POST"])
    def reset_password(token: str):
        settings = store.load_settings()
        if _normalize_users(settings):
            store.save_settings(settings)

        user = next((u for u in settings["users"] if u.get("reset_token") == token), None)
        now = datetime.now(timezone.utc)
        if not user:
            flash("This password reset link is invalid.", "error")
            return redirect(url_for("login"))
        expires_at = user.get("reset_expires_at")
        if not expires_at or datetime.fromisoformat(expires_at) < now:
            flash("This password reset link has expired.", "error")
            return redirect(url_for("forgot_password"))

        if request.method == "POST":
            new_password = request.form.get("new_password", "").strip()
            confirm_password = request.form.get("confirm_password", "").strip()
            if len(new_password) < 8:
                flash("Password must be at least 8 characters.", "error")
            elif new_password != confirm_password:
                flash("Passwords do not match.", "error")
            else:
                user["password"] = new_password
                user["force_password_change"] = False
                user["reset_token"] = None
                user["reset_expires_at"] = None
                store.save_settings(settings)
                flash("Password has been reset. Please sign in.", "success")
                return redirect(url_for("login"))
        return render_template("reset_password.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        if not session.get("user"):
            return redirect(url_for("login"))
        return redirect(url_for("dashboard"))

    @app.get("/service-worker.js")
    def service_worker():
        response = app.send_static_file("service-worker.js")
        response.headers["Service-Worker-Allowed"] = "/"
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api/push/config")
    def push_config():
        guard = _require(roles=("maintenance", "admin"))
        if guard:
            return guard
        return jsonify({"publicKey": app.config["VAPID_PUBLIC_KEY"]})

    @app.post("/api/push/subscriptions")
    def save_push_subscription():
        guard = _require(roles=("maintenance", "admin"))
        if guard:
            return guard
        subscription = request.get_json(silent=True) or {}
        if not subscription.get("endpoint") or not subscription.get("keys"):
            return jsonify({"error": "A valid push subscription is required."}), 400
        location = _location_for_current_user()
        if not location:
            session.clear()
            return jsonify({"error": "Your account is no longer available."}), 403
        store.save_push_subscription(subscription, session["user"], session["role"], location)
        return jsonify({"ok": True}), 201

    @app.get("/dashboard")
    def dashboard():
        guard = _require()
        if guard:
            return guard
        role = session["role"]

        if role == "admin":
            return redirect(url_for("admin"))

        location = _location_for_current_user()
        if not location:
            session.clear()
            return redirect(url_for("login"))
        issues = [issue for issue in store.load_issues() if issue.get("location", DEFAULT_LOCATION) == location]
        open_issues = sorted(
            (i for i in issues if i["status"] == "open"),
            key=lambda x: x["created_at"],
        )

        if role == "maintenance":
            return render_template("maintenance_dashboard.html", open_issues=open_issues)

        cutoff = (datetime.now(timezone.utc) - timedelta(weeks=2)).isoformat()
        recent_closed = sorted(
            (i for i in issues if i["status"] == "closed" and (i.get("closed_at") or "") >= cutoff),
            key=lambda x: x.get("closed_at", ""),
            reverse=True,
        )
        return render_template(
            "frontdesk_dashboard.html",
            open_issues=open_issues,
            recent_closed=recent_closed,
        )

    @app.get("/history")
    def history():
        guard = _require(roles=("maintenance", "front_desk", "supervisor", "admin"))
        if guard:
            return guard
        location = _location_for_current_user()
        if not location:
            session.clear()
            return redirect(url_for("login"))
        issues = [issue for issue in store.load_issues() if issue.get("location", DEFAULT_LOCATION) == location]
        closed = sorted(
            (i for i in issues if i["status"] == "closed"),
            key=lambda x: x.get("closed_at", ""),
            reverse=True,
        )
        by_room: dict[str, list] = {}
        for issue in closed:
            by_room.setdefault(issue["room"], []).append(issue)

        def room_sort_key(room: str):
            digits = "".join(ch for ch in str(room) if ch.isdigit())
            return (0, int(digits), str(room)) if digits else (1, str(room))

        return render_template(
            "history.html",
            by_room=by_room,
            rooms=sorted(by_room, key=room_sort_key),
        )

    @app.post("/issues")
    def create_issue():
        guard = _require(roles=("front_desk", "supervisor", "maintenance", "admin"))
        if guard:
            return guard
        room = request.form.get("room", "").strip()
        description = request.form.get("description", "").strip()
        if not room or not description:
            flash("Room and description are required.", "error")
            return redirect(url_for("dashboard"))
        location = _location_for_current_user()
        if not location:
            session.clear()
            return redirect(url_for("login"))
        issue = store.add_issue(room, description, session["user"], location)
        _send_new_issue_notifications(issue)
        flash(f"Issue submitted for room {room.upper()}.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/issues/<issue_id>/close")
    def close_issue(issue_id):
        guard = _require(roles=("maintenance", "admin"))
        if guard:
            return guard
        resolution = request.form.get("resolution", "").strip()
        if not resolution:
            flash("Resolution is required before marking an issue complete.", "error")
            return redirect(url_for("dashboard"))
        location = _location_for_current_user()
        if not location:
            session.clear()
            return redirect(url_for("login"))
        if not store.close_issue(issue_id, session["user"], resolution, location):
            flash("Issue was not found at your assigned location.", "error")
        return redirect(url_for("dashboard"))

    @app.route("/admin", methods=["GET", "POST"])
    def admin():
        guard = _require(roles=("admin",))
        if guard:
            return guard
        settings = store.load_settings()
        if _normalize_users(settings):
            store.save_settings(settings)

        if request.method == "POST":
            action = request.form.get("action")

            if action == "add_user":
                username = request.form.get("username", "").strip().lower()
                password = request.form.get("password", "").strip()
                email = request.form.get("email", "").strip().lower()
                role = request.form.get("role", "").strip()
                location = request.form.get("location", "").strip()
                if not username or not password or not email or role not in ("maintenance", "front_desk", "supervisor"):
                    flash("Username, email, password, location, and valid role are required.", "error")
                elif location not in settings["locations"]:
                    flash("Select a configured location.", "error")
                elif any(u["username"].lower() == username for u in settings["users"]):
                    flash("Username already exists.", "error")
                else:
                    settings["users"].append(
                        {
                            "username": username,
                            "email": email,
                            "password": password,
                            "role": role,
                            "location": location,
                            "force_password_change": True,
                            "reset_token": None,
                            "reset_expires_at": None,
                        }
                    )
                    store.save_settings(settings)
                    flash(f"User '{username}' added.", "success")
                return redirect(url_for("admin"))

            if action == "add_location":
                location = request.form.get("location", "").strip()
                if not location:
                    flash("Location name is required.", "error")
                elif any(existing.casefold() == location.casefold() for existing in settings["locations"]):
                    flash("That location already exists.", "error")
                else:
                    settings["locations"].append(location)
                    store.save_settings(settings)
                    flash(f"Location '{location}' added.", "success")
                return redirect(url_for("admin"))

            if action == "change_location":
                username = request.form.get("username", "").strip()
                location = request.form.get("location", "").strip()
                if location not in settings["locations"]:
                    flash("Select a configured location.", "error")
                else:
                    user = next((u for u in settings["users"] if u["username"] == username), None)
                    if not user:
                        flash("User was not found.", "error")
                    else:
                        user["location"] = location
                        store.save_settings(settings)
                        flash(f"Location updated for '{username}'.", "success")
                return redirect(url_for("admin"))

            if action == "change_role":
                username = request.form.get("username", "").strip()
                role = request.form.get("role", "").strip()
                if role not in ("maintenance", "front_desk", "supervisor"):
                    flash("Select a valid role.", "error")
                else:
                    user = next((u for u in settings["users"] if u["username"] == username), None)
                    if not user:
                        flash("User was not found.", "error")
                    elif user["role"] == "admin":
                        flash("Admin roles cannot be changed.", "error")
                    else:
                        user["role"] = role
                        store.save_settings(settings)
                        flash(f"Role updated for '{username}'.", "success")
                return redirect(url_for("admin"))

            if action == "delete_user":
                username = request.form.get("username", "").strip()
                if username == session["user"]:
                    flash("Cannot delete your own account.", "error")
                else:
                    settings["users"] = [u for u in settings["users"] if u["username"] != username]
                    store.save_settings(settings)
                    flash(f"User '{username}' deleted.", "success")
                return redirect(url_for("admin"))

            if action == "change_password":
                username = request.form.get("username", "").strip()
                new_password = request.form.get("new_password", "").strip()
                if not new_password:
                    flash("New password required.", "error")
                else:
                    for u in settings["users"]:
                        if u["username"] == username:
                            u["password"] = new_password
                            break
                    store.save_settings(settings)
                    flash(f"Password updated for '{username}'.", "success")
                return redirect(url_for("admin"))

        return render_template("admin.html", users=settings["users"], locations=settings["locations"])

    @app.route("/supervisor", methods=["GET", "POST"])
    def supervisor():
        """Manage the current location's non-admin accounts and maintenance reports."""
        guard = _require(roles=("supervisor",))
        if guard:
            return guard
        location = _location_for_current_user()
        if not location:
            session.clear()
            return redirect(url_for("login"))
        settings = store.load_settings()
        if _normalize_users(settings):
            store.save_settings(settings)

        if request.method == "POST":
            action = request.form.get("action")
            username = request.form.get("username", "").strip().lower()
            managed_user = next(
                (
                    user for user in settings["users"]
                    if user["username"].lower() == username
                    and user.get("location") == location
                    and user.get("role") != "admin"
                ),
                None,
            )
            if action == "add_user":
                password = request.form.get("password", "").strip()
                email = request.form.get("email", "").strip().lower()
                role = request.form.get("role", "").strip()
                if not username or not password or not email or role not in ("maintenance", "front_desk", "supervisor"):
                    flash("Username, email, password, and a valid role are required.", "error")
                elif any(user["username"].lower() == username for user in settings["users"]):
                    flash("Username already exists.", "error")
                else:
                    settings["users"].append(
                        {
                            "username": username,
                            "email": email,
                            "password": password,
                            "role": role,
                            "location": location,
                            "force_password_change": True,
                            "reset_token": None,
                            "reset_expires_at": None,
                        }
                    )
                    store.save_settings(settings)
                    flash(f"User '{username}' added to {location}.", "success")
            elif action == "change_password":
                new_password = request.form.get("new_password", "").strip()
                if not managed_user:
                    flash("User was not found at your location.", "error")
                elif not new_password:
                    flash("New password required.", "error")
                else:
                    managed_user["password"] = new_password
                    managed_user["force_password_change"] = True
                    store.save_settings(settings)
                    flash(f"Password reset for '{managed_user['username']}'.", "success")
            elif action == "delete_user":
                if not managed_user:
                    flash("User was not found at your location.", "error")
                elif managed_user["username"] == session["user"]:
                    flash("Cannot delete your own account.", "error")
                else:
                    settings["users"].remove(managed_user)
                    store.save_settings(settings)
                    flash(f"User '{managed_user['username']}' deleted.", "success")
            return redirect(url_for("supervisor"))

        users = [
            user
            for user in settings["users"]
            if user.get("location") == location and user.get("role") != "admin"
        ]
        issues = [issue for issue in store.load_issues() if issue.get("location", DEFAULT_LOCATION) == location]
        return render_template("supervisor.html", users=users, location=location, issues=issues)

    @app.get("/supervisor/report.csv")
    def supervisor_report_csv():
        guard = _require(roles=("supervisor",))
        if guard:
            return guard
        location = _location_for_current_user()
        if not location:
            session.clear()
            return redirect(url_for("login"))
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["Room", "Description", "Status", "Reported", "Reported by", "Resolved", "Resolved by", "Resolution"])
        issues = sorted(
            (item for item in store.load_issues() if item.get("location", DEFAULT_LOCATION) == location),
            key=lambda item: item["created_at"],
        )
        for issue in issues:
            writer.writerow(
                [
                    issue["room"], issue["description"], issue["status"], issue["created_at"], issue["created_by"],
                    issue.get("closed_at") or "", issue.get("closed_by") or "", issue.get("resolution") or "",
                ]
            )
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{location}-maintenance-report.csv"'},
        )

    return app


if __name__ == "__main__":
    app = create_app()
    host = os.getenv("WEB_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", os.getenv("WEB_PORT", "7070")))
    app.run(host=host, port=port)
