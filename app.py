import csv
import hmac
import json
import logging
import os
import secrets
import tempfile
import uuid
from io import StringIO
from base64 import urlsafe_b64encode
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from urllib import error, request as urllib_request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from flask import Flask, Response, flash, jsonify, redirect, render_template, request, session, url_for
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush
from werkzeug.security import check_password_hash, generate_password_hash

from api import register_api

DEFAULT_LOCATION = "HCK"
ISSUE_CATEGORIES = ("guest", "maintenance")
DEFAULT_CATEGORY = "maintenance"  # category assumed for issues saved before categories existed
DEFAULT_NEW_ISSUE_CATEGORY = "guest"  # tab and form choice everyone starts on
MAX_ISSUES_PER_SUBMIT = 20

# Accounts created on a brand-new deployment. They carry no password here: the
# store generates a random one for each when it first writes settings.json,
# logs it once, and forces a change at first sign-in.
DEFAULT_USERS = (
    ("admin", "admin"),
    ("maintenance", "maintenance"),
    ("frontdesk", "front_desk"),
    ("supervisor", "supervisor"),
)

DEFAULT_SETTINGS = {
    "locations": [DEFAULT_LOCATION],
    "users": [
        {"username": username, "password": None, "role": role, "email": "", "location": DEFAULT_LOCATION, "force_password_change": True}
        for username, role in DEFAULT_USERS
    ],
}

INSECURE_SECRET_KEYS = {"", "change-me"}

# Prefixes produced by werkzeug.security.generate_password_hash.
_PASSWORD_HASH_PREFIXES = ("scrypt:", "pbkdf2:")


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def is_password_hash(value) -> bool:
    return isinstance(value, str) and value.startswith(_PASSWORD_HASH_PREFIXES)


def verify_password(stored, candidate: str) -> tuple[bool, bool]:
    """Check ``candidate`` against a stored password.

    Returns ``(matches, needs_rehash)``. ``needs_rehash`` is True when the stored
    value is a legacy plaintext password that matched and should be re-saved
    as a hash.
    """
    if not isinstance(stored, str) or not stored or not candidate:
        return False, False
    if is_password_hash(stored):
        try:
            return check_password_hash(stored, candidate), False
        except (ValueError, TypeError):
            return False, False
    matches = hmac.compare_digest(stored.encode("utf-8"), candidate.encode("utf-8"))
    return matches, matches


def hash_plaintext_passwords(settings: dict) -> bool:
    """Replace any legacy plaintext passwords in ``settings`` with hashes."""
    changed = False
    for user in settings.get("users", []):
        password = user.get("password")
        if isinstance(password, str) and password and not is_password_hash(password):
            user["password"] = hash_password(password)
            changed = True
    return changed


_vapid_key_creation_lock = Lock()


class Store:
    def __init__(self, config_dir: str):
        self.config_dir = Path(config_dir)
        self.settings_file = self.config_dir / "settings.json"
        self.issues_file = self.config_dir / "issues.json"
        self.push_subscriptions_file = self.config_dir / "push_subscriptions.json"
        self.vapid_private_key_file = self.config_dir / "vapid_private.pem"
        self.secret_key_file = self.config_dir / "secret_key"
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def _write(self, path: Path, data) -> None:
        # Write to a temporary file and rename it into place so that another
        # worker never reads a half-written file.
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _seed_settings(self) -> None:
        """Create settings.json with the default accounts if it does not exist.

        Each default account gets a random password, stored hashed and logged
        once, and must change it at first sign-in. Creation is exclusive so that
        concurrent workers never log passwords that were not the ones kept.
        """
        data = deepcopy(DEFAULT_SETTINGS)
        initial_passwords = {}
        for user in data["users"]:
            password = secrets.token_urlsafe(12)
            initial_passwords[user["username"]] = password
            user["password"] = hash_password(password)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.config_dir), prefix=".settings.json.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.link(tmp_name, self.settings_file)
        except FileExistsError:
            return
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        logging.getLogger(__name__).warning(
            "Created %s with default accounts. Initial passwords (shown once; each must be changed at first sign-in): %s",
            self.settings_file,
            ", ".join(f"{name}={password}" for name, password in initial_passwords.items()),
        )

    def load_settings(self) -> dict:
        if not self.settings_file.exists():
            self._seed_settings()
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

    def load_or_create_secret_key(self) -> str:
        """Return a persistent random Flask secret key stored under the config dir."""
        if not self.secret_key_file.exists():
            fd, tmp_name = tempfile.mkstemp(dir=str(self.config_dir), prefix=".secret_key.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(secrets.token_hex(32))
                os.chmod(tmp_name, 0o600)
                # link() fails if the file already exists, so concurrent workers
                # agree on whichever key was written first.
                os.link(tmp_name, self.secret_key_file)
            except FileExistsError:
                pass
            finally:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
        key = self.secret_key_file.read_text(encoding="utf-8").strip()
        if len(key) < 32:
            raise RuntimeError(f"{self.secret_key_file} does not contain a usable secret key.")
        return key

    def add_issue(
        self,
        room: str,
        description: str,
        created_by: str,
        location: str,
        category: str = DEFAULT_CATEGORY,
        checkout_date: str | None = None,
    ) -> dict:
        issues = self.load_issues()
        issue = {
            "id": str(uuid.uuid4()),
            "room": room.strip().upper(),
            "description": description.strip(),
            "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": created_by,
            "location": location,
            "category": category if category in ISSUE_CATEGORIES else DEFAULT_CATEGORY,
            "do_on_checkout": bool(checkout_date),
            "checkout_date": checkout_date or None,
            "closed_at": None,
            "closed_by": None,
            "resolution": None,
        }
        issues.append(issue)
        self.save_issues(issues)
        return issue

    def move_issue(self, issue_id: str, category: str, location: str) -> dict | None:
        """Move an open issue to another category, returning the updated issue."""
        issues = self.load_issues()
        for issue in issues:
            if (
                issue["id"] == issue_id
                and issue["status"] == "open"
                and issue.get("location", DEFAULT_LOCATION) == location
            ):
                issue["category"] = category
                if category != "guest":
                    # "On checkout" only applies to guest issues.
                    issue["do_on_checkout"] = False
                    issue["checkout_date"] = None
                self.save_issues(issues)
                return issue
        return None

    def update_issue(
        self,
        issue_id: str,
        location: str,
        edited_by: str,
        room: str,
        description: str,
        category: str,
        checkout_date: str | None,
    ) -> dict | None:
        """Edit an open issue's details, returning the updated issue."""
        issues = self.load_issues()
        for issue in issues:
            if (
                issue["id"] == issue_id
                and issue["status"] == "open"
                and issue.get("location", DEFAULT_LOCATION) == location
            ):
                issue["room"] = room.strip().upper()
                issue["description"] = description.strip()
                issue["category"] = category
                # "On checkout" only applies to guest issues.
                issue["checkout_date"] = checkout_date if category == "guest" else None
                issue["do_on_checkout"] = bool(issue["checkout_date"])
                issue["edited_at"] = datetime.now(timezone.utc).isoformat()
                issue["edited_by"] = edited_by
                self.save_issues(issues)
                return issue
        return None

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
                # Ends any running labor or parts wait for response-time reporting.
                issue.setdefault("events", []).append({"type": "finish", "at": issue["closed_at"], "by": closed_by})
                issue["resolution"] = resolution.strip()
                self.save_issues(issues)
                return True
        return False


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def create_app() -> Flask:
    app = Flask(__name__)
    testing = _env_flag("TESTING")
    app.config["TESTING"] = testing

    store = Store(os.getenv("CONFIG_PATH", "/data"))
    app.config["STORE"] = store

    secret_key = os.getenv("SECRET_KEY", "").strip()
    if secret_key in INSECURE_SECRET_KEYS:
        # Never fall back to a well-known key: anyone could forge sessions.
        # Use a random key persisted under CONFIG_PATH so sessions survive
        # restarts and are shared by every gunicorn worker.
        secret_key = store.load_or_create_secret_key()
        if not testing:
            app.logger.warning(
                "SECRET_KEY is not set; using the generated key in %s. Set SECRET_KEY to manage it yourself.",
                store.secret_key_file,
            )
    app.secret_key = secret_key

    if not (app.debug or testing) and os.getenv("SESSION_COOKIE_SECURE", "").strip().lower() not in ("0", "false", "no", "off"):
        app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="Lax")
    else:
        app.config.update(SESSION_COOKIE_SAMESITE="Lax")

    # Seed a new deployment's accounts, or hash any passwords still stored in
    # plaintext by older versions.
    settings = store.load_settings()
    if hash_plaintext_passwords(settings):
        store.save_settings(settings)
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

    def _issues_for_location(location: str) -> list[dict]:
        """Return only issues assigned to one exact hotel location.

        Location names are identifiers, not prefixes: for example, a report for
        ``HCK`` must not include issues assigned to ``HCK2``.
        """
        return [
            issue
            for issue in store.load_issues()
            if issue.get("location", DEFAULT_LOCATION) == location
        ]

    def _room_sort_key(room: str):
        digits = "".join(ch for ch in str(room) if ch.isdigit())
        return (0, int(digits), str(room)) if digits else (1, str(room))

    def _issue_category(issue: dict) -> str:
        """Issues saved before categories existed are treated as maintenance issues."""
        category = issue.get("category", DEFAULT_CATEGORY)
        return category if category in ISSUE_CATEGORIES else DEFAULT_CATEGORY

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
            user = next((u for u in settings["users"] if u["username"].lower() == username), None)
            matches, needs_rehash = verify_password(user.get("password") if user else None, password)
            if user and matches:
                if needs_rehash:
                    user["password"] = hash_password(password)
                    store.save_settings(settings)
                session.clear()
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
                user["password"] = hash_password(new_password)
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
                user["password"] = hash_password(new_password)
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

    # Public pages linked from the App Store listing; no sign-in required.
    @app.get("/privacy")
    def privacy():
        return render_template("privacy.html", support_email=os.getenv("SUPPORT_EMAIL", "").strip())

    @app.get("/support")
    def support():
        return render_template("support.html", support_email=os.getenv("SUPPORT_EMAIL", "").strip())

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
        issues = _issues_for_location(location)
        default_tab = DEFAULT_NEW_ISSUE_CATEGORY
        tab = request.args.get("tab", default_tab)
        if tab not in ISSUE_CATEGORIES:
            tab = default_tab
        # The chosen sort sticks for the session so it survives form submissions.
        sort = request.args.get("sort") or session.get("issue_sort", "default")
        if sort not in ("default", "room"):
            sort = "default"
        session["issue_sort"] = sort
        all_open = sorted(
            (i for i in issues if i["status"] == "open"),
            key=lambda x: x["created_at"],
        )
        if sort == "room":
            all_open.sort(key=lambda x: _room_sort_key(x["room"]))
        tab_counts = {
            category: sum(1 for i in all_open if _issue_category(i) == category)
            for category in ISSUE_CATEGORIES
        }
        open_by_category = {
            category: [i for i in all_open if _issue_category(i) == category]
            for category in ISSUE_CATEGORIES
        }

        if role == "maintenance":
            return render_template(
                "maintenance_dashboard.html",
                open_by_category=open_by_category,
                tab=tab,
                sort=sort,
                tab_counts=tab_counts,
                default_category=default_tab,
            )

        cutoff = (datetime.now(timezone.utc) - timedelta(weeks=2)).isoformat()
        recent_closed = sorted(
            (i for i in issues if i["status"] == "closed" and (i.get("closed_at") or "") >= cutoff),
            key=lambda x: x.get("closed_at", ""),
            reverse=True,
        )
        if sort == "room":
            recent_closed.sort(key=lambda x: _room_sort_key(x["room"]))
        closed_by_category = {
            category: [i for i in recent_closed if _issue_category(i) == category]
            for category in ISSUE_CATEGORIES
        }
        return render_template(
            "frontdesk_dashboard.html",
            open_by_category=open_by_category,
            closed_by_category=closed_by_category,
            tab=tab,
            sort=sort,
            tab_counts=tab_counts,
            default_category=default_tab,
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
        issues = _issues_for_location(location)
        closed = sorted(
            (i for i in issues if i["status"] == "closed"),
            key=lambda x: x.get("closed_at", ""),
            reverse=True,
        )
        by_room: dict[str, list] = {}
        for issue in closed:
            by_room.setdefault(issue["room"], []).append(issue)

        return render_template(
            "history.html",
            by_room=by_room,
            rooms=sorted(by_room, key=_room_sort_key),
        )

    @app.post("/issues")
    def create_issue():
        guard = _require(roles=("front_desk", "supervisor", "maintenance", "admin"))
        if guard:
            return guard
        room = request.form.get("room", "").strip()
        default_category = DEFAULT_NEW_ISSUE_CATEGORY
        # Each row is one issue for the room; its fields carry the row id as a suffix.
        # A form without row ids is treated as a single issue using the plain field names.
        row_ids = request.form.getlist("row")[:MAX_ISSUES_PER_SUBMIT] or [""]
        entries = []
        for row_id in row_ids:
            suffix = f"_{row_id}" if row_id else ""
            description = request.form.get(f"description{suffix}", "").strip()
            category = request.form.get(f"category{suffix}", "").strip()
            if category not in ISSUE_CATEGORIES:
                category = default_category
            if not room or not description:
                flash("Room and a description for every issue are required.", "error")
                return redirect(url_for("dashboard", tab=category))
            checkout_date = None
            if category == "guest" and request.form.get(f"do_on_checkout{suffix}"):
                raw_date = request.form.get(f"checkout_date{suffix}", "").strip()
                try:
                    checkout_date = datetime.strptime(raw_date, "%Y-%m-%d").date().isoformat()
                except ValueError:
                    flash("Select a valid checkout date.", "error")
                    return redirect(url_for("dashboard", tab=category))
            entries.append((description, category, checkout_date))
        location = _location_for_current_user()
        if not location:
            session.clear()
            return redirect(url_for("login"))
        for description, category, checkout_date in entries:
            issue = store.add_issue(room, description, session["user"], location, category, checkout_date)
            _send_new_issue_notifications(issue)
        if len(entries) == 1:
            flash(f"Issue submitted for room {room.upper()}.", "success")
        else:
            flash(f"{len(entries)} issues submitted for room {room.upper()}.", "success")
        return redirect(url_for("dashboard", tab=entries[0][1]))

    @app.get("/api/room-issues")
    def room_issues():
        """Open issues and resolved history for one room at the user's location."""
        if session.get("role") not in ("front_desk", "supervisor", "maintenance", "admin"):
            return jsonify({"error": "Access denied."}), 403
        room = request.args.get("room", "").strip().upper()
        if not room:
            return jsonify({"error": "Room is required."}), 400
        location = _location_for_current_user()
        if not location:
            session.clear()
            return jsonify({"error": "Your account is no longer available."}), 403
        room_items = [i for i in _issues_for_location(location) if str(i["room"]).upper() == room]

        def summarize(issue: dict) -> dict:
            return {
                "category": _issue_category(issue),
                "description": issue["description"],
                "checkout_date": issue.get("checkout_date"),
                "created_at": fmt_date(issue["created_at"]),
                "created_by": issue["created_by"],
                "closed_at": fmt_date(issue.get("closed_at")),
                "closed_by": issue.get("closed_by"),
                "resolution": issue.get("resolution"),
            }

        open_items = sorted((i for i in room_items if i["status"] == "open"), key=lambda x: x["created_at"])
        closed_items = sorted(
            (i for i in room_items if i["status"] == "closed"),
            key=lambda x: x.get("closed_at") or "",
            reverse=True,
        )
        return jsonify({
            "room": room,
            "open": [summarize(i) for i in open_items],
            "history": [summarize(i) for i in closed_items],
        })

    @app.post("/issues/<issue_id>/edit")
    def edit_issue(issue_id):
        if session.get("role") not in ("front_desk", "supervisor", "maintenance", "admin"):
            return jsonify({"error": "You cannot edit issues."}), 403
        room = request.form.get("room", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "").strip()
        if not room or not description:
            return jsonify({"error": "Room and description are required."}), 400
        if category not in ISSUE_CATEGORIES:
            return jsonify({"error": "Select a valid category."}), 400
        checkout_date = None
        if category == "guest" and request.form.get("do_on_checkout"):
            try:
                checkout_date = datetime.strptime(request.form.get("checkout_date", "").strip(), "%Y-%m-%d").date().isoformat()
            except ValueError:
                return jsonify({"error": "Select a valid checkout date."}), 400
        location = _location_for_current_user()
        if not location:
            session.clear()
            return jsonify({"error": "Your account is no longer available."}), 403
        issue = store.update_issue(issue_id, location, session["user"], room, description, category, checkout_date)
        if not issue:
            return jsonify({"error": "Issue was not found at your assigned location."}), 404
        return jsonify({"ok": True, "issue": {
            "room": issue["room"], "description": issue["description"],
            "category": issue["category"], "checkout_date": issue["checkout_date"],
        }})

    @app.post("/issues/<issue_id>/move")
    def move_issue(issue_id):
        if session.get("role") not in ("supervisor", "admin", "maintenance"):
            return jsonify({"error": "You cannot move issues."}), 403
        category = request.form.get("category", "").strip()
        if category not in ISSUE_CATEGORIES:
            return jsonify({"error": "Select a valid category."}), 400
        location = _location_for_current_user()
        if not location:
            session.clear()
            return jsonify({"error": "Your account is no longer available."}), 403
        issue = store.move_issue(issue_id, category, location)
        if not issue:
            return jsonify({"error": "Issue was not found at your assigned location."}), 404
        return jsonify({"ok": True, "category": issue["category"]})

    @app.post("/issues/<issue_id>/close")
    def close_issue(issue_id):
        # The dashboard submits this with fetch so the page keeps its scroll position;
        # a plain form post still works and redirects back to the dashboard.
        wants_json = request.headers.get("X-Requested-With") == "fetch"
        if wants_json and session.get("role") not in ("maintenance", "admin"):
            return jsonify({"error": "You cannot complete issues."}), 403
        guard = _require(roles=("maintenance", "admin"))
        if guard:
            return guard
        resolution = request.form.get("resolution", "").strip()
        if not resolution:
            if wants_json:
                return jsonify({"error": "Resolution is required before marking an issue complete."}), 400
            flash("Resolution is required before marking an issue complete.", "error")
            return redirect(url_for("dashboard"))
        location = _location_for_current_user()
        if not location:
            session.clear()
            if wants_json:
                return jsonify({"error": "Your account is no longer available."}), 403
            return redirect(url_for("login"))
        if not store.close_issue(issue_id, session["user"], resolution, location):
            if wants_json:
                return jsonify({"error": "Issue was not found at your assigned location."}), 404
            flash("Issue was not found at your assigned location.", "error")
            return redirect(url_for("dashboard"))
        if wants_json:
            return jsonify({"ok": True})
        return redirect(url_for("dashboard"))

    register_api(app, store, SimpleNamespace(
        current_user=_current_user,
        location_for_current_user=_location_for_current_user,
        issues_for_location=_issues_for_location,
        issue_category=_issue_category,
        normalize_users=_normalize_users,
        send_new_issue_notifications=_send_new_issue_notifications,
        verify_password=verify_password,
        hash_password=hash_password,
        ISSUE_CATEGORIES=ISSUE_CATEGORIES,
        DEFAULT_NEW_ISSUE_CATEGORY=DEFAULT_NEW_ISSUE_CATEGORY,
        MAX_ISSUES_PER_SUBMIT=MAX_ISSUES_PER_SUBMIT,
    ))

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
                            "password": hash_password(password),
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
                            u["password"] = hash_password(new_password)
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
                            "password": hash_password(password),
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
                    managed_user["password"] = hash_password(new_password)
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
        issues = _issues_for_location(location)
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
        writer.writerow(["Room", "Type", "On checkout", "Description", "Status", "Reported", "Reported by", "Resolved", "Resolved by", "Resolution"])
        issues = sorted(
            _issues_for_location(location),
            key=lambda item: item["created_at"],
        )
        for issue in issues:
            writer.writerow(
                [
                    issue["room"], _issue_category(issue), issue.get("checkout_date") or "",
                    issue["description"], issue["status"], issue["created_at"], issue["created_by"],
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
