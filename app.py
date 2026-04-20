import json
import os
import re
from copy import deepcopy
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
    "properties": [
        {
            "id": "default-property",
            "name": "Default Property",
            "receiving_addresses": "",
            "smtp_server": "smtp.gmail.com",
            "smtp_port": 587,
            "smtp_username": "",
            "smtp_password": "",
            "smtp_use_tls": True,
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

        # Backward compatibility with old single-forum/single-email settings.
        legacy_forum = data.get("forum")
        legacy_email = data.get("email")
        legacy_notification = data.get("notification")

        if isinstance(legacy_forum, dict):
            username = str(legacy_forum.get("username", "")).strip()
            password = str(legacy_forum.get("password", "")).strip()
            if username and password:
                settings["users"] = [
                    {
                        "username": username,
                        "password": password,
                        "properties": [settings["properties"][0]["id"]],
                    }
                ]

        if isinstance(legacy_email, dict):
            settings["properties"][0].update(
                {
                    "receiving_addresses": str(
                        legacy_email.get("email_address", "")
                    ).strip(),
                    "smtp_server": str(legacy_email.get("smtp_server", "")).strip(),
                    "smtp_port": to_int(legacy_email.get("smtp_port"), 587),
                    "smtp_username": str(legacy_email.get("email_address", "")).strip(),
                    "smtp_password": str(legacy_email.get("password", "")).strip(),
                    "smtp_use_tls": bool(legacy_email.get("use_tls", True)),
                }
            )

        if isinstance(legacy_notification, dict):
            settings["properties"][0].update(
                {
                    "weekly_report_emails": str(
                        legacy_notification.get("weekly_report_email", "")
                    ).strip(),
                    "weekly_report_day": str(
                        legacy_notification.get("send_day", "monday")
                    ).strip(),
                    "weekly_report_time": str(
                        legacy_notification.get("send_time", "09:00")
                    ).strip(),
                    "job_completion_cc": str(
                        legacy_notification.get("cc_emails", "")
                    ).strip(),
                }
            )

        users = data.get("users")
        if isinstance(users, list):
            settings["users"] = []
            for user in users:
                if not isinstance(user, dict):
                    continue
                username = str(user.get("username", "")).strip()
                password = str(user.get("password", "")).strip()
                properties = user.get("properties", [])
                if not username or not password:
                    continue
                if not isinstance(properties, list):
                    properties = []
                settings["users"].append(
                    {
                        "username": username,
                        "password": password,
                        "properties": [str(prop) for prop in properties],
                    }
                )

        properties = data.get("properties")
        if isinstance(properties, list):
            settings["properties"] = []
            for prop in properties:
                if not isinstance(prop, dict):
                    continue
                name = str(prop.get("name", "")).strip()
                prop_id = make_slug(str(prop.get("id") or name))
                if not name:
                    continue
                settings["properties"].append(
                    {
                        "id": prop_id,
                        "name": name,
                        "receiving_addresses": str(
                            prop.get("receiving_addresses", "")
                        ).strip(),
                        "smtp_server": str(prop.get("smtp_server", "")).strip(),
                        "smtp_port": to_int(prop.get("smtp_port"), 587),
                        "smtp_username": str(prop.get("smtp_username", "")).strip(),
                        "smtp_password": str(prop.get("smtp_password", "")).strip(),
                        "smtp_use_tls": bool(prop.get("smtp_use_tls", True)),
                        "weekly_report_emails": str(
                            prop.get("weekly_report_emails", "")
                        ).strip(),
                        "weekly_report_day": str(
                            prop.get("weekly_report_day", "monday")
                        ).strip(),
                        "weekly_report_time": str(
                            prop.get("weekly_report_time", "09:00")
                        ).strip(),
                        "job_completion_cc": str(
                            prop.get("job_completion_cc", "")
                        ).strip(),
                    }
                )

        property_ids = {prop["id"] for prop in settings["properties"]}
        for user in settings["users"]:
            user["properties"] = [
                prop_id for prop_id in user["properties"] if prop_id in property_ids
            ]

        if not settings["users"]:
            settings["users"] = deepcopy(DEFAULT_SETTINGS["users"])
        if not settings["properties"]:
            settings["properties"] = deepcopy(DEFAULT_SETTINGS["properties"])
        return settings

    def save(self, settings: dict) -> None:
        with self.config_file.open("w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)


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

    @app.get("/")
    def index():
        if not session.get("forum_authenticated"):
            return redirect(url_for("forum_login"))

        settings = app.config["SETTINGS_STORE"].load()
        assigned_property_ids = session.get("forum_properties", [])
        assigned_property_names = property_names_for_ids(
            settings["properties"], assigned_property_ids
        )
        return render_template(
            "forum_home.html", assigned_property_names=assigned_property_names
        )

    @app.route("/forum/login", methods=["GET", "POST"])
    def forum_login():
        if request.method == "POST":
            settings = app.config["SETTINGS_STORE"].load()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "").strip()

            matched_user = next(
                (
                    user
                    for user in settings["users"]
                    if user["username"] == username and user["password"] == password
                ),
                None,
            )
            if matched_user:
                session["forum_authenticated"] = True
                session["forum_username"] = matched_user["username"]
                session["forum_properties"] = matched_user.get("properties", [])
                return redirect(url_for("index"))

            flash("Invalid forum username or password.", "error")
        return render_template("forum_login.html")

    @app.post("/forum/logout")
    def forum_logout():
        session.pop("forum_authenticated", None)
        session.pop("forum_username", None)
        session.pop("forum_properties", None)
        return redirect(url_for("forum_login"))

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")

            if (
                username == app.config["ADMIN_USERNAME"]
                and password == app.config["ADMIN_PASSWORD"]
            ):
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
                if not username or not password:
                    flash("New users require both username and password.", "error")
                elif any(user["username"] == username for user in settings_data["users"]):
                    flash("Usernames must be unique.", "error")
                else:
                    settings_data["users"].append(
                        {"username": username, "password": password, "properties": []}
                    )
                    store.save(settings_data)
                    flash("User added.", "success")
                    return redirect(url_for("settings"))
            elif action == "add_property":
                name = request.form.get("new_property_name", "").strip()
                if not name:
                    flash("Property name is required.", "error")
                else:
                    prop_id = make_slug(name)
                    existing_ids = {prop["id"] for prop in settings_data["properties"]}
                    base_id = prop_id
                    counter = 2
                    while prop_id in existing_ids:
                        prop_id = f"{base_id}-{counter}"
                        counter += 1
                    settings_data["properties"].append(
                        {
                            "id": prop_id,
                            "name": name,
                            "receiving_addresses": "",
                            "smtp_server": "smtp.gmail.com",
                            "smtp_port": 587,
                            "smtp_username": "",
                            "smtp_password": "",
                            "smtp_use_tls": True,
                            "weekly_report_emails": "",
                            "weekly_report_day": "monday",
                            "weekly_report_time": "09:00",
                            "job_completion_cc": "",
                        }
                    )
                    store.save(settings_data)
                    flash("Property added.", "success")
                    return redirect(url_for("settings"))
            else:
                updated = {"users": [], "properties": []}

                for index, existing_user in enumerate(settings_data["users"]):
                    if request.form.get(f"user_{index}_remove") == "on":
                        continue
                    username = request.form.get(
                        f"user_{index}_username", existing_user["username"]
                    ).strip()
                    password = request.form.get(
                        f"user_{index}_password", existing_user["password"]
                    ).strip()
                    assigned = request.form.getlist(f"user_{index}_properties")
                    if username and password:
                        updated["users"].append(
                            {
                                "username": username,
                                "password": password,
                                "properties": assigned,
                            }
                        )

                used_usernames = set()
                unique_users = []
                for user in updated["users"]:
                    if user["username"] in used_usernames:
                        continue
                    unique_users.append(user)
                    used_usernames.add(user["username"])
                updated["users"] = unique_users

                for index, existing_property in enumerate(settings_data["properties"]):
                    if request.form.get(f"property_{index}_remove") == "on":
                        continue
                    prop_name = request.form.get(
                        f"property_{index}_name", existing_property["name"]
                    ).strip()
                    prop_id = existing_property["id"]
                    if not prop_name:
                        continue
                    updated["properties"].append(
                        {
                            "id": prop_id,
                            "name": prop_name,
                            "receiving_addresses": request.form.get(
                                f"property_{index}_receiving_addresses", ""
                            ).strip(),
                            "smtp_server": request.form.get(
                                f"property_{index}_smtp_server", ""
                            ).strip(),
                            "smtp_port": to_int(
                                request.form.get(f"property_{index}_smtp_port"), 587
                            ),
                            "smtp_username": request.form.get(
                                f"property_{index}_smtp_username", ""
                            ).strip(),
                            "smtp_password": request.form.get(
                                f"property_{index}_smtp_password", ""
                            ).strip(),
                            "smtp_use_tls": request.form.get(
                                f"property_{index}_smtp_use_tls"
                            )
                            == "on",
                            "weekly_report_emails": request.form.get(
                                f"property_{index}_weekly_report_emails", ""
                            ).strip(),
                            "weekly_report_day": request.form.get(
                                f"property_{index}_weekly_report_day", "monday"
                            ).strip(),
                            "weekly_report_time": request.form.get(
                                f"property_{index}_weekly_report_time", "09:00"
                            ).strip(),
                            "job_completion_cc": request.form.get(
                                f"property_{index}_job_completion_cc", ""
                            ).strip(),
                        }
                    )

                property_ids = {prop["id"] for prop in updated["properties"]}
                for user in updated["users"]:
                    user["properties"] = [
                        prop_id for prop_id in user["properties"] if prop_id in property_ids
                    ]

                if not updated["users"]:
                    flash("At least one forum user is required.", "error")
                elif not updated["properties"]:
                    flash("At least one property is required.", "error")
                else:
                    store.save(updated)
                    flash("Settings saved.", "success")
                    return redirect(url_for("settings"))

        property_name_by_id = {
            property_item["id"]: property_item["name"]
            for property_item in settings_data["properties"]
        }
        return render_template(
            "settings.html",
            settings=settings_data,
            property_name_by_id=property_name_by_id,
        )

    return app


if __name__ == "__main__":
    app = create_app()
    host = os.getenv("WEB_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", os.getenv("WEB_PORT", "7070")))
    app.run(host=host, port=port)
