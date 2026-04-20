import json
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, session, url_for

DEFAULT_SETTINGS = {
    "users": [
        {"username": "admin", "password": "admin123", "role": "admin"},
        {"username": "maintenance", "password": "changeme", "role": "maintenance"},
        {"username": "frontdesk", "password": "changeme", "role": "front_desk"},
    ]
}


class Store:
    def __init__(self, config_dir: str):
        self.config_dir = Path(config_dir)
        self.settings_file = self.config_dir / "settings.json"
        self.issues_file = self.config_dir / "issues.json"
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

    def add_issue(self, room: str, description: str, created_by: str) -> dict:
        issues = self.load_issues()
        issue = {
            "id": str(uuid.uuid4()),
            "room": room.strip().upper(),
            "description": description.strip(),
            "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": created_by,
            "closed_at": None,
            "closed_by": None,
            "resolution": None,
        }
        issues.append(issue)
        self.save_issues(issues)
        return issue

    def close_issue(self, issue_id: str, closed_by: str, resolution: str) -> bool:
        issues = self.load_issues()
        for issue in issues:
            if issue["id"] == issue_id and issue["status"] == "open":
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

    @app.template_filter("fmtdate")
    def fmt_date(value):
        if not value:
            return "—"
        try:
            dt = datetime.fromisoformat(str(value))
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
                return redirect(url_for("dashboard"))
            flash("Invalid username or password.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        if not session.get("user"):
            return redirect(url_for("login"))
        return redirect(url_for("dashboard"))

    @app.get("/dashboard")
    def dashboard():
        guard = _require()
        if guard:
            return guard
        role = session["role"]

        if role == "admin":
            return redirect(url_for("admin"))

        issues = store.load_issues()
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
        guard = _require(roles=("maintenance", "admin"))
        if guard:
            return guard
        issues = store.load_issues()
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
        guard = _require(roles=("front_desk", "admin"))
        if guard:
            return guard
        room = request.form.get("room", "").strip()
        description = request.form.get("description", "").strip()
        if not room or not description:
            flash("Room and description are required.", "error")
            return redirect(url_for("dashboard"))
        store.add_issue(room, description, session["user"])
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
        store.close_issue(issue_id, session["user"], resolution)
        return redirect(url_for("dashboard"))

    @app.route("/admin", methods=["GET", "POST"])
    def admin():
        guard = _require(roles=("admin",))
        if guard:
            return guard
        settings = store.load_settings()

        if request.method == "POST":
            action = request.form.get("action")

            if action == "add_user":
                username = request.form.get("username", "").strip().lower()
                password = request.form.get("password", "").strip()
                role = request.form.get("role", "").strip()
                if not username or not password or role not in ("maintenance", "front_desk"):
                    flash("Username, password, and valid role are required.", "error")
                elif any(u["username"].lower() == username for u in settings["users"]):
                    flash("Username already exists.", "error")
                else:
                    settings["users"].append({"username": username, "password": password, "role": role})
                    store.save_settings(settings)
                    flash(f"User '{username}' added.", "success")
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

        return render_template("admin.html", users=settings["users"])

    return app


if __name__ == "__main__":
    app = create_app()
    host = os.getenv("WEB_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", os.getenv("WEB_PORT", "7070")))
    app.run(host=host, port=port)
