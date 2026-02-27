import json
import os
import re
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, session, url_for

DEFAULT_SETTINGS = {
    "users": [
        {
            "username": "maintainer",
            "password": "changeme",
            "properties": ["default-property"],
        }
    ],
    "frontdesk_users": [
        {
            "username": "frontdesk",
            "password": "changeme",
            "properties": ["default-property"],
        }
    ],
    "properties": [
        {
            "id": "default-property",
            "name": "Default Property",
            "weekly_report_emails": "",
            "weekly_report_day": "monday",
            "weekly_report_time": "09:00",
            "job_completion_cc": "",
        }
    ],
}


def make_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "property"


def to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def format_display_datetime(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return ""
    return parsed.strftime("%d%b%y %H:%M").upper()


def prepare_recent_logs(logs: list[dict]) -> list[dict]:
    formatted_logs = []
    for job in logs:
        row = deepcopy(job)
        row["room"] = row.get("location") or row.get("room_number") or ""
        row["created_at_display"] = format_display_datetime(row.get("created_time"))
        row["completed_at_display"] = format_display_datetime(row.get("completion_time"))
        formatted_logs.append(row)
    return formatted_logs


class SettingsStore:
    def __init__(self, config_dir: str):
        self.config_dir = Path(config_dir)
        self.config_file = self.config_dir / "settings.json"
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        puid = os.getenv("PUID")
        pgid = os.getenv("PGID")
        if os.geteuid() == 0 and puid and pgid:
            try:
                os.chown(self.config_dir, int(puid), int(pgid))
            except (PermissionError, ValueError):
                pass

    def load(self) -> dict:
        if not self.config_file.exists():
            settings = deepcopy(DEFAULT_SETTINGS)
            self.save(settings)
            return settings

        try:
            with self.config_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}

        return self._normalize(data)

    def _normalize(self, data: dict) -> dict:
        settings = deepcopy(DEFAULT_SETTINGS)

        users = data.get("users")
        if isinstance(users, list):
            settings["users"] = []
            for user in users:
                if not isinstance(user, dict):
                    continue
                username = str(user.get("username", "")).strip()
                password = str(user.get("password", "")).strip()
                properties = user.get("properties", [])
                if username and password:
                    settings["users"].append(
                        {
                            "username": username,
                            "password": password,
                            "properties": properties if isinstance(properties, list) else [],
                        }
                    )

        frontdesk_users = data.get("frontdesk_users")
        if isinstance(frontdesk_users, list):
            settings["frontdesk_users"] = []
            for user in frontdesk_users:
                if not isinstance(user, dict):
                    continue
                username = str(user.get("username", "")).strip()
                password = str(user.get("password", "")).strip()
                properties = user.get("properties", [])
                if username and password:
                    settings["frontdesk_users"].append(
                        {
                            "username": username,
                            "password": password,
                            "properties": properties if isinstance(properties, list) else [],
                        }
                    )

        properties = data.get("properties")
        if isinstance(properties, list):
            settings["properties"] = []
            for prop in properties:
                if not isinstance(prop, dict):
                    continue
                name = str(prop.get("name", "")).strip()
                if not name:
                    continue
                prop_id = make_slug(str(prop.get("id") or name))
                settings["properties"].append(
                    {
                        "id": prop_id,
                        "name": name,
                        "weekly_report_emails": str(prop.get("weekly_report_emails", "")).strip(),
                        "weekly_report_day": str(prop.get("weekly_report_day", "monday")).strip(),
                        "weekly_report_time": str(prop.get("weekly_report_time", "09:00")).strip(),
                        "job_completion_cc": str(prop.get("job_completion_cc", "")).strip(),
                    }
                )

        if not settings["users"]:
            settings["users"] = deepcopy(DEFAULT_SETTINGS["users"])
        if not settings["frontdesk_users"]:
            settings["frontdesk_users"] = deepcopy(DEFAULT_SETTINGS["frontdesk_users"])
        if not settings["properties"]:
            settings["properties"] = deepcopy(DEFAULT_SETTINGS["properties"])

        property_ids = {prop["id"] for prop in settings["properties"]}
        for user_group in ["users", "frontdesk_users"]:
            for user in settings[user_group]:
                user["properties"] = [pid for pid in user["properties"] if pid in property_ids]

        return settings

    def save(self, settings: dict) -> None:
        with self.config_file.open("w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)


class JobsStore:
    def __init__(self, jobs_file: str):
        self.jobs_file = Path(jobs_file)
        self.jobs_file.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        if not self.jobs_file.exists():
            return {}
        try:
            with self.jobs_file.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

        jobs = {}
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            row = deepcopy(value)
            row["job_number"] = str(row.get("job_number", key))
            row.setdefault("room_number", "")
            row.setdefault("location", row.get("room_number", ""))
            row.setdefault("issue", "")
            row.setdefault("status", "Pending")
            row.setdefault("created_time", datetime.now().isoformat())
            row.setdefault("completion_time", None)
            row.setdefault("resolution", "")
            row.setdefault("completed_by", "")
            row.setdefault("created_by", "")
            jobs[str(key)] = row
        return jobs

    def save(self, jobs: dict) -> None:
        with self.jobs_file.open("w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=2)

    def _next_number(self, jobs: dict) -> str:
        if not jobs:
            return "1"
        return str(max(int(k) for k in jobs.keys()) + 1)

    def create_job(self, location: str, issue: str, created_by: str) -> str:
        jobs = self.load()
        job_number = self._next_number(jobs)
        now = datetime.now().isoformat()
        jobs[job_number] = {
            "job_number": job_number,
            "room_number": location,
            "location": location,
            "issue": issue,
            "status": "Pending",
            "created_time": now,
            "completion_time": None,
            "resolution": "",
            "completed_by": "",
            "created_by": created_by,
        }
        self.save(jobs)
        return job_number

    def complete_job(self, job_number: str, completed_by: str, resolution: str) -> bool:
        jobs = self.load()
        target = jobs.get(str(job_number))
        if not target:
            return False
        target["status"] = "Completed"
        target["completion_time"] = datetime.now().isoformat()
        target["resolution"] = resolution.strip()
        target["completed_by"] = completed_by
        jobs[str(job_number)] = target
        self.save(jobs)
        return True

    def get_open_jobs(self) -> list[dict]:
        jobs = self.load()
        open_jobs = [job for job in jobs.values() if job.get("status") != "Completed"]
        return sorted(open_jobs, key=lambda item: item.get("created_time") or "", reverse=True)

    def get_recent_logs(self, days: int = 14) -> list[dict]:
        cutoff = datetime.now() - timedelta(days=days)
        logs = []
        for job in self.load().values():
            completion = job.get("completion_time")
            if not completion:
                continue
            try:
                completed_at = datetime.fromisoformat(completion)
            except (TypeError, ValueError):
                continue
            if completed_at >= cutoff:
                logs.append(job)
        return sorted(logs, key=lambda item: item.get("completion_time") or "", reverse=True)


def property_names_for_ids(properties: list[dict], property_ids: list[str]) -> list[str]:
    names_by_id = {item["id"]: item["name"] for item in properties}
    return [names_by_id[prop_id] for prop_id in property_ids if prop_id in names_by_id]


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.getenv("SECRET_KEY", "change-me")

    app.config["ADMIN_USERNAME"] = os.getenv("ADMIN_USERNAME", "admin")
    app.config["ADMIN_PASSWORD"] = os.getenv("ADMIN_PASSWORD", "admin123")

    config_dir = os.getenv("CONFIG_PATH", "/config")
    app.config["SETTINGS_STORE"] = SettingsStore(config_dir)
    app.config["JOBS_STORE"] = JobsStore(os.getenv("JOBS_FILE", str(Path(config_dir) / "jobs.json")))

    @app.get("/")
    def index():
        if session.get("frontdesk_authenticated"):
            return redirect(url_for("frontdesk_home"))
        if session.get("forum_authenticated"):
            settings = app.config["SETTINGS_STORE"].load()
            assigned_property_ids = session.get("forum_properties", [])
            assigned_property_names = property_names_for_ids(settings["properties"], assigned_property_ids)
            jobs_store = app.config["JOBS_STORE"]
            return render_template(
                "forum_home.html",
                assigned_property_names=assigned_property_names,
                open_jobs=jobs_store.get_open_jobs(),
                recent_logs=prepare_recent_logs(jobs_store.get_recent_logs(days=14)),
            )
        return redirect(url_for("frontdesk_login"))

    @app.route("/frontdesk/login", methods=["GET", "POST"])
    def frontdesk_login():
        if request.method == "POST":
            settings = app.config["SETTINGS_STORE"].load()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "").strip()
            matched = next(
                (
                    user
                    for user in settings["frontdesk_users"]
                    if user["username"] == username and user["password"] == password
                ),
                None,
            )
            if matched:
                session["frontdesk_authenticated"] = True
                session["frontdesk_username"] = matched["username"]
                session["frontdesk_properties"] = matched.get("properties", [])
                return redirect(url_for("frontdesk_home"))
            flash("Invalid front desk username or password.", "error")
        return render_template("frontdesk_login.html")

    @app.post("/frontdesk/logout")
    def frontdesk_logout():
        session.pop("frontdesk_authenticated", None)
        session.pop("frontdesk_username", None)
        session.pop("frontdesk_properties", None)
        return redirect(url_for("frontdesk_login"))

    @app.get("/frontdesk")
    def frontdesk_home():
        if not session.get("frontdesk_authenticated"):
            return redirect(url_for("frontdesk_login"))
        settings = app.config["SETTINGS_STORE"].load()
        assigned_property_names = property_names_for_ids(
            settings["properties"], session.get("frontdesk_properties", [])
        )
        jobs_store = app.config["JOBS_STORE"]
        open_jobs = jobs_store.get_open_jobs()
        recent_logs = prepare_recent_logs(jobs_store.get_recent_logs(days=14))
        return render_template(
            "frontdesk_home.html",
            assigned_property_names=assigned_property_names,
            open_jobs=open_jobs,
            recent_logs=recent_logs,
        )

    @app.post("/frontdesk/jobs/new")
    def frontdesk_create_job():
        if not session.get("frontdesk_authenticated"):
            return redirect(url_for("frontdesk_login"))
        location = request.form.get("location", "").strip()
        issue = request.form.get("issue", "").strip()
        if not location or not issue:
            flash("Location and issue are required.", "error")
            return redirect(url_for("frontdesk_home"))
        created_by = session.get("frontdesk_username", "frontdesk")
        job_number = app.config["JOBS_STORE"].create_job(location, issue, created_by)
        flash(f"Maintenance request #{job_number} created.", "success")
        return redirect(url_for("frontdesk_home"))

    @app.route("/forum/login", methods=["GET", "POST"])
    def forum_login():
        if request.method == "POST":
            settings = app.config["SETTINGS_STORE"].load()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "").strip()
            matched = next(
                (
                    user
                    for user in settings["users"]
                    if user["username"] == username and user["password"] == password
                ),
                None,
            )
            if matched:
                session["forum_authenticated"] = True
                session["forum_username"] = matched["username"]
                session["forum_properties"] = matched.get("properties", [])
                return redirect(url_for("index"))
            flash("Invalid forum username or password.", "error")
        return render_template("forum_login.html")

    @app.post("/forum/logout")
    def forum_logout():
        session.pop("forum_authenticated", None)
        session.pop("forum_username", None)
        session.pop("forum_properties", None)
        return redirect(url_for("forum_login"))

    @app.post("/jobs/<job_number>/complete")
    def complete_job(job_number: str):
        if not session.get("forum_authenticated"):
            return redirect(url_for("forum_login"))
        completed_by = session.get("forum_username", "")
        resolution = request.form.get("resolution", "").strip()
        if app.config["JOBS_STORE"].complete_job(job_number, completed_by, resolution):
            flash(f"Job #{job_number} marked completed by {completed_by}.", "success")
        else:
            flash(f"Job #{job_number} not found.", "error")
        return redirect(url_for("index"))

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if username == app.config["ADMIN_USERNAME"] and password == app.config["ADMIN_PASSWORD"]:
                session["admin_authenticated"] = True
                return redirect(url_for("settings"))
            flash("Invalid admin credentials.", "error")
        return render_template("admin_login.html")

    @app.post("/admin/logout")
    def admin_logout():
        session.pop("admin_authenticated", None)
        return redirect(url_for("admin_login"))

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if not session.get("admin_authenticated"):
            return redirect(url_for("admin_login"))

        store = app.config["SETTINGS_STORE"]
        settings_data = store.load()

        if request.method == "POST":
            action = request.form.get("action", "save")
            if action == "add_user":
                username = request.form.get("new_username", "").strip()
                password = request.form.get("new_password", "").strip()
                if username and password:
                    settings_data["users"].append({"username": username, "password": password, "properties": []})
                    store.save(settings_data)
                    flash("Maintenance user added.", "success")
                    return redirect(url_for("settings"))
                flash("Username and password required.", "error")
            elif action == "add_frontdesk_user":
                username = request.form.get("new_frontdesk_username", "").strip()
                password = request.form.get("new_frontdesk_password", "").strip()
                if username and password:
                    settings_data["frontdesk_users"].append({"username": username, "password": password, "properties": []})
                    store.save(settings_data)
                    flash("Front desk user added.", "success")
                    return redirect(url_for("settings"))
                flash("Username and password required.", "error")
            elif action == "add_property":
                name = request.form.get("new_property_name", "").strip()
                if name:
                    settings_data["properties"].append(
                        {
                            "id": make_slug(name),
                            "name": name,
                            "weekly_report_emails": "",
                            "weekly_report_day": "monday",
                            "weekly_report_time": "09:00",
                            "job_completion_cc": "",
                        }
                    )
                    store.save(settings_data)
                    flash("Property added.", "success")
                    return redirect(url_for("settings"))
                flash("Property name required.", "error")
            else:
                updated = {"users": [], "frontdesk_users": [], "properties": []}

                for i, existing in enumerate(settings_data["users"]):
                    if request.form.get(f"user_{i}_remove") == "on":
                        continue
                    username = request.form.get(f"user_{i}_username", existing["username"]).strip()
                    password = request.form.get(f"user_{i}_password", existing["password"]).strip()
                    if username and password:
                        updated["users"].append(
                            {
                                "username": username,
                                "password": password,
                                "properties": request.form.getlist(f"user_{i}_properties"),
                            }
                        )

                for i, existing in enumerate(settings_data["frontdesk_users"]):
                    if request.form.get(f"frontdesk_user_{i}_remove") == "on":
                        continue
                    username = request.form.get(
                        f"frontdesk_user_{i}_username", existing["username"]
                    ).strip()
                    password = request.form.get(
                        f"frontdesk_user_{i}_password", existing["password"]
                    ).strip()
                    if username and password:
                        updated["frontdesk_users"].append(
                            {
                                "username": username,
                                "password": password,
                                "properties": request.form.getlist(f"frontdesk_user_{i}_properties"),
                            }
                        )

                for i, existing in enumerate(settings_data["properties"]):
                    if request.form.get(f"property_{i}_remove") == "on":
                        continue
                    name = request.form.get(f"property_{i}_name", existing["name"]).strip()
                    if not name:
                        continue
                    updated["properties"].append(
                        {
                            "id": existing["id"],
                            "name": name,
                            "weekly_report_emails": request.form.get(
                                f"property_{i}_weekly_report_emails", ""
                            ).strip(),
                            "weekly_report_day": request.form.get(
                                f"property_{i}_weekly_report_day", "monday"
                            ).strip(),
                            "weekly_report_time": request.form.get(
                                f"property_{i}_weekly_report_time", "09:00"
                            ).strip(),
                            "job_completion_cc": request.form.get(
                                f"property_{i}_job_completion_cc", ""
                            ).strip(),
                        }
                    )

                property_ids = {prop["id"] for prop in updated["properties"]}
                for group in ["users", "frontdesk_users"]:
                    for user in updated[group]:
                        user["properties"] = [pid for pid in user["properties"] if pid in property_ids]

                if updated["users"] and updated["frontdesk_users"] and updated["properties"]:
                    store.save(updated)
                    flash("Settings saved.", "success")
                    return redirect(url_for("settings"))
                flash("At least one maintenance user, front desk user, and property are required.", "error")

        return render_template("settings.html", settings=settings_data)

    return app


if __name__ == "__main__":
    app = create_app()
    host = os.getenv("WEB_HOST", "0.0.0.0")
    port = to_int(os.getenv("WEB_PORT", "7070"), 7070)
    app.run(host=host, port=port)
