"""JSON API used by the HotelCraft iOS app.

Same session cookie, roles and location scoping as the web pages, but every
request and response is JSON so the app never has to parse HTML.

Work on an issue is recorded as an append-only list of events so response,
labor and parts-wait times can be derived later:

    start / stop             labor intervals
    awaiting_parts           job is on hold until parts_received
    finish                   written when the issue is closed (web or app)
"""

import json
import statistics
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import threading
from threading import Lock
from types import SimpleNamespace

import httpx
from flask import Flask, jsonify, request, session

from apns import DEAD_TOKEN_REASONS, APNsSender, Devices, new_issue_payload
from pm import DEFAULT_PM_ITEMS, RATINGS, PMRecords, clean_items, quarter_bounds, quarter_of

ALL_ROLES = ("front_desk", "supervisor", "maintenance", "admin")
WORK_ROLES = ("maintenance", "admin")
INVENTORY_ROLES = ("maintenance", "supervisor", "admin")
REPORT_ROLES = ("supervisor", "admin")
PROPERTY_ROLES = ("supervisor", "admin")
PM_ROLES = ("maintenance", "supervisor", "admin")
MAX_ROOMS = 3000
MAX_ROOM_LENGTH = 12
WORK_EVENTS = ("start", "stop", "awaiting_parts", "parts_received")
NOTIFY_ROLES = ("maintenance", "admin")  # who gets a new-job notification, same as the web push

_inventory_lock = Lock()
_property_lock = Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def work_state(issue: dict) -> str:
    """new, active, paused or awaiting_parts — replayed from the event log."""
    state = "new"
    for event in issue.get("events", []):
        kind = event["type"]
        if kind == "start":
            state = "active"
        elif kind == "stop":
            state = "paused"
        elif kind == "awaiting_parts":
            state = "awaiting_parts"
        elif kind == "parts_received":
            state = "paused"
    return state


def work_metrics(issue: dict) -> dict:
    """Minutes for response (created → first start, or → close if never started),
    active labor, awaiting parts, and total resolution."""
    created = _parse(issue["created_at"])
    closed = _parse(issue.get("closed_at"))
    now = datetime.now(timezone.utc)
    labor = parts_wait = timedelta()
    first_start = labor_from = parts_from = None
    for event in issue.get("events", []):
        at = _parse(event["at"])
        kind = event["type"]
        if kind == "start" and labor_from is None:
            labor_from = at
            first_start = first_start or at
        elif kind in ("stop", "awaiting_parts", "finish") and labor_from is not None:
            labor += at - labor_from
            labor_from = None
        if kind == "awaiting_parts" and parts_from is None:
            parts_from = at
        elif kind in ("parts_received", "finish") and parts_from is not None:
            parts_wait += at - parts_from
            parts_from = None
    end = closed or now
    if labor_from is not None:
        labor += end - labor_from
    if parts_from is not None:
        parts_wait += end - parts_from

    def minutes(delta: timedelta | None) -> float | None:
        return None if delta is None else round(delta.total_seconds() / 60, 1)

    responded = first_start or closed
    return {
        "response_minutes": minutes(responded - created) if responded else None,
        "labor_minutes": minutes(labor),
        "parts_wait_minutes": minutes(parts_wait),
        "resolution_minutes": minutes(closed - created) if closed else None,
    }


class Inventory:
    """Per-location parts stock in inventory.json next to the other data files."""

    def __init__(self, config_dir: str):
        self.path = Path(config_dir) / "inventory.json"

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return json.load(f)

    def save(self, items: list[dict]) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)


class Properties:
    """Per-location property details (name, address, phone, room list) in properties.json,
    keyed by location."""

    def __init__(self, config_dir: str):
        self.path = Path(config_dir) / "properties.json"

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        with self.path.open(encoding="utf-8") as f:
            return json.load(f)

    def save(self, data: dict) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


def room_sort_key(room: str):
    """Numeric rooms in number order ("98" before "214"), then anything else alphabetically."""
    digits = "".join(c for c in room if c.isdigit())
    return (0, int(digits), room) if digits else (1, 0, room)


def clean_rooms(raw) -> tuple[list[str] | None, str | None]:
    """Uppercase, de-duplicate and sort a room list; returns (rooms, error)."""
    if not isinstance(raw, list):
        return None, "Rooms must be a list."
    rooms = set()
    for value in raw:
        room = str(value or "").strip().upper()
        if not room:
            continue
        if len(room) > MAX_ROOM_LENGTH or not all(c.isalnum() or c in "-" for c in room):
            return None, f"'{room}' isn't a valid room number."
        rooms.add(room)
    if len(rooms) > MAX_ROOMS:
        return None, f"A property can have at most {MAX_ROOMS} rooms."
    return sorted(rooms, key=room_sort_key), None


def expand_room_text(text: str) -> tuple[list[str] | None, str | None]:
    """Typed room entry: "101-130, 201 305B" → 101…130, 201, 305B (as in the app)."""
    rooms = []
    for token in str(text or "").replace(";", ",").replace(",", " ").split():
        low, dash, high = token.partition("-")
        if dash and low.isdigit() and high.isdigit():
            if int(low) > int(high):
                return None, f"{token}: the first room must be lower."
            if int(high) - int(low) >= 500:
                return None, f"{token} is more than 500 rooms."
            rooms += [str(n).zfill(len(low)) for n in range(int(low), int(high) + 1)]
        else:
            rooms.append(token)
    return rooms, None


MAX_ROOM_TYPE_LENGTH = 12


def clean_room_type(value) -> tuple[str | None, str | None]:
    """A room type code such as KXTD; blank clears it. Returns (code, error)."""
    code = str(value or "").strip().upper()
    if not code:
        return None, None
    if len(code) > MAX_ROOM_TYPE_LENGTH or not all(c.isalnum() or c in "-_/" for c in code):
        return None, f"'{code}' isn't a valid room type code."
    return code, None


def parse_room_type_lines(text: str) -> tuple[list[tuple[list[str], str | None]] | None, str | None]:
    """Web form lines like "101-110 KXTD" or "115 SXBV" → [(rooms, code)]."""
    entries = []
    for line in str(text or "").splitlines():
        parts = line.replace(",", " ").split()
        if not parts:
            continue
        if len(parts) < 2:
            return None, f"'{line.strip()}' needs a room and a type code, like 101-110 KXTD."
        rooms, message = expand_room_text(" ".join(parts[:-1]))
        if message:
            return None, message
        code, message = clean_room_type(parts[-1])
        if message:
            return None, message
        entries.append(([r.upper() for r in rooms], code))
    return entries, None


def room_type_lines(rooms: list[str], types: dict) -> str:
    """The reverse, for prefilling the web form: runs of numbered rooms with one code become ranges."""
    lines, run = [], []

    def flush():
        if run:
            first, last, code = run[0], run[-1], types[run[0]]
            lines.append(f"{first}-{last} {code}" if len(run) > 1 else f"{first} {code}")
            run.clear()

    for room in rooms:
        code = types.get(room)
        if not code:
            flush()
            continue
        if run and room.isdigit() and run[-1].isdigit() and int(room) == int(run[-1]) + 1 \
                and len(room) == len(run[-1]) and types[run[-1]] == code:
            run.append(room)
        else:
            flush()
            run.append(room)
    flush()
    return "\n".join(lines)


MAX_AREAS = 200
MAX_AREA_LENGTH = 40


def place_key(text: str) -> str:
    """How a typed room or area is compared: case, spacing and a leading "Room" don't matter."""
    key = " ".join(str(text or "").split()).casefold()
    return key[5:] if key.startswith("room ") else key


def clean_areas(raw, rooms: list[str]) -> tuple[list[str] | None, str | None]:
    """Public areas (Lobby, Pool, Exterior…): trimmed, de-duplicated, never the same as a room."""
    if not isinstance(raw, list):
        return None, "Areas must be a list."
    room_keys = {place_key(r) for r in rooms}
    areas, seen = [], set()
    for value in raw:
        area = " ".join(str(value or "").split())
        if not area or place_key(area) in seen:
            continue
        if len(area) > MAX_AREA_LENGTH:
            return None, f"'{area[:20]}…' is too long for an area name."
        if place_key(area) in room_keys:
            return None, f"'{area}' is already a room."
        seen.add(place_key(area))
        areas.append(area)
    if len(areas) > MAX_AREAS:
        return None, f"A property can have at most {MAX_AREAS} areas."
    return sorted(areas, key=str.casefold), None


def register_api(app: Flask, store, h: SimpleNamespace) -> None:
    """Add /api/v1 routes. ``h`` carries helpers defined inside create_app:
    current_user, location_for_current_user, issues_for_location, issue_category,
    normalize_users, send_new_issue_notifications, verify_password, hash_password, and the constants
    DEFAULT_LOCATION, ISSUE_CATEGORIES, DEFAULT_NEW_ISSUE_CATEGORY, MAX_ISSUES_PER_SUBMIT, URGENT_ROLES."""
    inventory = Inventory(store.config_dir)
    properties = Properties(store.config_dir)
    pm_records = PMRecords(store.config_dir)
    devices = Devices(store.config_dir)
    app.config.setdefault("APNS_SENDER", APNsSender.from_env())
    if not app.config["APNS_SENDER"]:
        app.logger.warning("iPhone app notifications are off: set APNS_AUTH_KEY, APNS_KEY_ID and APNS_TEAM_ID.")

    def notify_app_devices(issue: dict) -> None:
        """Alert signed-in iPhones at the issue's location. Runs in the background so
        creating a request never waits on Apple."""
        sender = app.config.get("APNS_SENDER")
        if not sender:
            return
        location = issue.get("location", h.DEFAULT_LOCATION)
        targets = [d for d in devices.load() if d.get("role") in NOTIFY_ROLES and d.get("location") == location]
        if not targets:
            return
        payload = new_issue_payload(issue)

        def deliver():
            dead = set()
            with httpx.Client(http2=True, timeout=10) as client:
                for device in targets:
                    try:
                        status, reason = sender.send(client, device["token"], payload, bool(device.get("sandbox")))
                    except httpx.HTTPError as exc:
                        app.logger.warning("APNs request failed: %s", exc)
                        continue
                    if status == 410 or reason in DEAD_TOKEN_REASONS:
                        dead.add(device["token"])
                    elif status != 200:
                        app.logger.warning("APNs rejected a notification: %s %s", status, reason)
            devices.remove(dead)

        if app.config.get("APNS_SYNC"):
            deliver()
        else:
            threading.Thread(target=deliver, daemon=True).start()

    app.extensions["notify_app_devices"] = notify_app_devices

    # ---- Rooms and areas a request can be for ----

    def places_for(location: str) -> tuple[list[str], list[str]]:
        record = properties.load().get(location) or {}
        return record.get("rooms") or [], record.get("areas") or []

    def resolve_place(location: str, text: str, current: str | None = None) -> tuple[str | None, str | None]:
        """Match what was typed to the property's rooms and areas; returns (canonical name, error).
        Until a supervisor sets up the room list, any text is accepted as before. An issue's
        existing room is always kept so older requests can still be edited."""
        rooms, areas = places_for(location)
        typed = " ".join(str(text or "").split())
        if not typed:
            return None, "Room is required."
        if current and place_key(typed) == place_key(current):
            return current, None
        if not rooms and not areas:
            return typed.upper(), None
        for place in rooms + areas:
            if place_key(place) == place_key(typed):
                return place.upper(), None
        return None, (f"'{typed}' isn't a room or area here. Pick one from the list, "
                      "or ask a supervisor to add it in Admin Settings.")

    app.extensions["resolve_place"] = resolve_place
    app.extensions["places_for"] = places_for
    app.extensions["room_type_lines"] = lambda location: room_type_lines(
        *places_for(location)[:1], (properties.load().get(location) or {}).get("room_types") or {})

    def save_places(location: str, rooms_text: str, areas_text: str, types_text: str = "") -> str | None:
        """The web supervisor page's Rooms & Areas form. Returns an error message, or None."""
        typed, message = expand_room_text(rooms_text)
        if message:
            return message
        rooms, message = clean_rooms(typed)
        if message:
            return message
        areas, message = clean_areas(str(areas_text or "").splitlines(), rooms)
        if message:
            return message
        entries, message = parse_room_type_lines(types_text)
        if message:
            return message
        types = {}
        for typed_rooms, code in entries:
            for room in typed_rooms:
                if room not in rooms:
                    return f"Room {room} has a type but isn't in the room list."
                if code:
                    types[room] = code
        with _property_lock:
            everything = properties.load()
            record = dict(everything.get(location) or {})
            record.update(rooms=rooms, areas=areas, room_types=types, updated_at=_now(), updated_by=session["user"])
            everything[location] = record
            properties.save(everything)
        return None

    app.extensions["save_places"] = save_places

    @app.context_processor
    def inject_places():
        """Room and area suggestions for the web forms' room field."""
        try:
            location = h.location_for_current_user() if session.get("user") else None
        except Exception:
            location = None
        if not location:
            return {"place_options": []}
        rooms, areas = places_for(location)
        return {"place_options": rooms + areas}

    def error(message: str, status: int):
        return jsonify({"error": message}), status

    def body() -> dict:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def api_issue(issue: dict) -> dict:
        parts = issue.get("parts", [])
        return {
            "id": issue["id"],
            "room": issue["room"],
            "description": issue["description"],
            "category": h.issue_category(issue),
            "status": issue["status"],
            "work_state": work_state(issue),
            "checkout_date": issue.get("checkout_date"),
            "urgent": bool(issue.get("urgent")),
            "created_at": issue["created_at"],
            "created_by": issue["created_by"],
            "edited_at": issue.get("edited_at"),
            "edited_by": issue.get("edited_by"),
            "closed_at": issue.get("closed_at"),
            "closed_by": issue.get("closed_by"),
            "resolution": issue.get("resolution"),
            "events": issue.get("events", []),
            "parts": parts,
            "parts_cost": round(sum(p["qty"] * p["unit_cost"] for p in parts), 2),
            "metrics": work_metrics(issue),
        }

    def me(user: dict) -> dict:
        return {
            "username": user["username"],
            "role": user["role"],
            "location": user.get("location"),
            "must_change_password": bool(user.get("force_password_change")),
        }

    def location_for(roles=ALL_ROLES):
        """Return (location, None) for an allowed signed-in user, or (None, error response)."""
        if not session.get("user"):
            return None, error("Sign in required.", 401)
        if session.get("role") not in roles:
            return None, error("Access denied.", 403)
        location = h.location_for_current_user()
        if not location:
            session.clear()
            return None, error("Your account is no longer available.", 401)
        return location, None

    def checkout_date(entry: dict, category: str) -> tuple[str | None, bool]:
        """Parse an optional YYYY-MM-DD checkout date; returns (date, ok)."""
        raw = str(entry.get("checkout_date") or "").strip()
        if category != "guest" or not raw:
            return None, True
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date().isoformat(), True
        except ValueError:
            return None, False

    def update_open_issue(issue_id: str, location: str, change):
        """Apply ``change(issue)`` to one open issue at ``location`` and save.
        ``change`` may return an error string to abort without saving."""
        issues = store.load_issues()
        issue = next(
            (
                i for i in issues
                if i["id"] == issue_id and i["status"] == "open"
                # Issues saved before locations existed belong to the default location, as on the web pages.
                and i.get("location", h.DEFAULT_LOCATION) == location
            ),
            None,
        )
        if not issue:
            return None, error("Issue was not found at your assigned location.", 404)
        problem = change(issue)
        if problem:
            return None, error(problem, 400)
        store.save_issues(issues)
        return issue, None

    # ---- Session ----

    @app.post("/api/v1/login")
    def api_login():
        data = body()
        username = str(data.get("username", "")).strip().lower()
        password = str(data.get("password", "")).strip()
        settings = store.load_settings()
        if h.normalize_users(settings):
            store.save_settings(settings)
        user = next((u for u in settings["users"] if u["username"].lower() == username), None)
        matches, needs_rehash = h.verify_password(user.get("password") if user else None, password)
        if not matches:
            return error("Invalid username or password.", 401)
        if needs_rehash:
            user["password"] = h.hash_password(password)
            store.save_settings(settings)
        session.permanent = True  # keep the app signed in across launches
        session["user"] = user["username"]
        session["role"] = user["role"]
        return jsonify(me(user))

    @app.post("/api/v1/logout")
    def api_logout():
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/v1/me")
    def api_me():
        user = h.current_user()
        if not user:
            session.clear()
            return error("Sign in required.", 401)
        return jsonify(me(user))

    @app.post("/api/v1/password")
    def api_change_password():
        if not session.get("user"):
            return error("Sign in required.", 401)
        new_password = str(body().get("new_password", "")).strip()
        if len(new_password) < 8:
            return error("Password must be at least 8 characters.", 400)
        settings = store.load_settings()
        user = next((u for u in settings["users"] if u["username"] == session["user"]), None)
        if not user:
            session.clear()
            return error("Your account is no longer available.", 401)
        user["password"] = h.hash_password(new_password)
        user["force_password_change"] = False
        store.save_settings(settings)
        return jsonify(me(user))

    # ---- Notifications ----

    @app.post("/api/v1/devices")
    def api_register_device():
        """{"token": "<hex APNs device token>", "sandbox": true for Xcode/debug builds}"""
        if not session.get("user"):
            return error("Sign in required.", 401)
        user = h.current_user()
        if not user:
            session.clear()
            return error("Your account is no longer available.", 401)
        data = body()
        token = str(data.get("token", "")).strip().lower()
        if not token or len(token) > 200 or any(c not in "0123456789abcdef" for c in token):
            return error("A valid device token is required.", 400)
        devices.register(token, user["username"], user["role"],
                         user.get("location") or h.DEFAULT_LOCATION, bool(data.get("sandbox")))
        return jsonify({"ok": True, "notifications": user["role"] in NOTIFY_ROLES})

    @app.delete("/api/v1/devices/<token>")
    def api_unregister_device(token):
        """Called when signing out so the phone stops getting this account's alerts."""
        if not session.get("user"):
            return error("Sign in required.", 401)
        token = token.strip().lower()
        if any(d["token"] == token and d["username"] == session["user"] for d in devices.load()):
            devices.remove({token})
        return jsonify({"ok": True})

    # ---- Issues ----

    @app.get("/api/v1/issues")
    def api_issues():
        """Open issues plus anything closed in the last two weeks at the user's location."""
        location, problem = location_for()
        if problem:
            return problem
        issues = h.issues_for_location(location)
        cutoff = (datetime.now(timezone.utc) - timedelta(weeks=2)).isoformat()
        open_items = sorted((i for i in issues if i["status"] == "open"), key=lambda x: x["created_at"])
        closed_items = sorted(
            (i for i in issues if i["status"] == "closed" and (i.get("closed_at") or "") >= cutoff),
            key=lambda x: x.get("closed_at") or "",
            reverse=True,
        )
        return jsonify({
            "open": [api_issue(i) for i in open_items],
            "recently_closed": [api_issue(i) for i in closed_items],
        })

    @app.post("/api/v1/issues")
    def api_create_issues():
        """Create one or more issues for a room: {"room": "214", "issues": [{"description", "category", "checkout_date"}]}."""
        location, problem = location_for()
        if problem:
            return problem
        data = body()
        entries = data.get("issues") or []
        if not str(data.get("room", "")).strip() or not isinstance(entries, list) or not entries:
            return error("Room and at least one issue are required.", 400)
        room, message = resolve_place(location, data.get("room"))
        if message:
            return error(message, 400)
        if len(entries) > h.MAX_ISSUES_PER_SUBMIT:
            return error(f"Submit at most {h.MAX_ISSUES_PER_SUBMIT} issues at once.", 400)
        parsed = []
        for entry in entries:
            entry = entry if isinstance(entry, dict) else {}
            description = str(entry.get("description", "")).strip()
            category = entry.get("category") if entry.get("category") in h.ISSUE_CATEGORIES else h.DEFAULT_NEW_ISSUE_CATEGORY
            if not description:
                return error("Every issue needs a description.", 400)
            # An urgent PM task goes straight to Guest Issues so it is worked first.
            urgent = bool(entry.get("urgent"))
            if urgent:
                if session.get("role") not in h.URGENT_ROLES:
                    return error("Only supervisors can mark a request urgent.", 403)
                category = "guest"
            date, ok = checkout_date(entry, category)
            if not ok:
                return error("Select a valid checkout date.", 400)
            parsed.append((description, category, None if urgent else date, urgent))
        created = []
        for description, category, date, urgent in parsed:
            issue = store.add_issue(room, description, session["user"], location, category, date, urgent)
            h.send_new_issue_notifications(issue)
            created.append(api_issue(issue))
        return jsonify({"issues": created}), 201

    @app.patch("/api/v1/issues/<issue_id>")
    def api_edit_issue(issue_id):
        location, problem = location_for()
        if problem:
            return problem
        data = body()
        room = str(data.get("room", "")).strip()
        description = str(data.get("description", "")).strip()
        category = data.get("category")
        if not room or not description:
            return error("Room and description are required.", 400)
        if category not in h.ISSUE_CATEGORIES:
            return error("Select a valid category.", 400)
        date, ok = checkout_date(data, category)
        if not ok:
            return error("Select a valid checkout date.", 400)
        existing = next((i for i in h.issues_for_location(location) if i["id"] == issue_id), None)
        room, message = resolve_place(location, room, existing["room"] if existing else None)
        if message:
            return error(message, 400)
        issue = store.update_issue(issue_id, location, session["user"], room, description, category, date)
        if not issue:
            return error("Issue was not found at your assigned location.", 404)
        return jsonify(api_issue(issue))

    @app.post("/api/v1/issues/<issue_id>/move")
    def api_move_issue(issue_id):
        location, problem = location_for(roles=("supervisor", "admin", "maintenance"))
        if problem:
            return problem
        category = body().get("category")
        if category not in h.ISSUE_CATEGORIES:
            return error("Select a valid category.", 400)
        issue = store.move_issue(issue_id, category, location)
        if not issue:
            return error("Issue was not found at your assigned location.", 404)
        return jsonify(api_issue(issue))

    @app.post("/api/v1/issues/<issue_id>/close")
    def api_close_issue(issue_id):
        location, problem = location_for(roles=WORK_ROLES)
        if problem:
            return problem
        resolution = str(body().get("resolution", "")).strip()
        if not resolution:
            return error("Resolution is required before marking an issue complete.", 400)
        if not store.close_issue(issue_id, session["user"], resolution, location):
            return error("Issue was not found at your assigned location.", 404)
        issue = next(i for i in store.load_issues() if i["id"] == issue_id)
        return jsonify(api_issue(issue))

    @app.post("/api/v1/issues/<issue_id>/events")
    def api_work_event(issue_id):
        """Start / stop work, or put the job on hold waiting for parts."""
        location, problem = location_for(roles=WORK_ROLES)
        if problem:
            return problem
        kind = body().get("type")
        if kind not in WORK_EVENTS:
            return error("Unknown work action.", 400)

        def apply(issue):
            state = work_state(issue)
            allowed = {
                "start": ("new", "paused"),
                "stop": ("active",),
                "awaiting_parts": ("new", "active", "paused"),
                "parts_received": ("awaiting_parts",),
            }[kind]
            if state not in allowed:
                return {
                    "start": "Mark parts received before starting work.",
                    "stop": "Work isn't running.",
                    "awaiting_parts": "Already awaiting parts.",
                    "parts_received": "This job isn't awaiting parts.",
                }[kind]
            issue.setdefault("events", []).append({"type": kind, "at": _now(), "by": session["user"]})
            return None

        issue, problem = update_open_issue(issue_id, location, apply)
        return problem or jsonify(api_issue(issue))

    @app.post("/api/v1/issues/<issue_id>/parts")
    def api_add_part(issue_id):
        """Use stock on a job: {"item_id": "...", "qty": 1}. Deducts from inventory."""
        location, problem = location_for(roles=WORK_ROLES)
        if problem:
            return problem
        data = body()
        try:
            qty = int(data.get("qty", 1))
        except (TypeError, ValueError):
            qty = 0
        if qty < 1:
            return error("Quantity must be at least 1.", 400)
        with _inventory_lock:
            items = inventory.load()
            item = next((i for i in items if i["id"] == data.get("item_id") and i["location"] == location), None)
            if not item:
                return error("That part isn't in inventory.", 404)
            if item["qty"] < qty:
                return error(f"Only {item['qty']} {item['name']} on hand. Update the count in Inventory first.", 400)

            def apply(issue):
                issue.setdefault("parts", []).append({
                    "id": str(uuid.uuid4()),
                    "item_id": item["id"],
                    "name": item["name"],
                    "barcode": item.get("barcode"),
                    "qty": qty,
                    "unit_cost": item.get("unit_cost", 0),
                    "added_by": session["user"],
                    "added_at": _now(),
                })
                return None

            issue, problem = update_open_issue(issue_id, location, apply)
            if problem:
                return problem
            item["qty"] -= qty
            inventory.save(items)
        return jsonify(api_issue(issue))

    @app.delete("/api/v1/issues/<issue_id>/parts/<usage_id>")
    def api_remove_part(issue_id, usage_id):
        """Take a part back off a job and return it to stock."""
        location, problem = location_for(roles=WORK_ROLES)
        if problem:
            return problem
        removed = {}

        def apply(issue):
            usage = next((p for p in issue.get("parts", []) if p["id"] == usage_id), None)
            if not usage:
                return "That part isn't on this job."
            issue["parts"].remove(usage)
            removed.update(usage)
            return None

        with _inventory_lock:
            issue, problem = update_open_issue(issue_id, location, apply)
            if problem:
                return problem
            items = inventory.load()
            item = next((i for i in items if i["id"] == removed["item_id"]), None)
            if item:
                item["qty"] += removed["qty"]
                inventory.save(items)
        return jsonify(api_issue(issue))

    @app.get("/api/v1/rooms/<room>")
    def api_room(room):
        """Open issues and full resolved history for one room."""
        location, problem = location_for()
        if problem:
            return problem
        room = room.strip().upper()
        items = [i for i in h.issues_for_location(location) if str(i["room"]).upper() == room]
        return jsonify({
            "room": room,
            "open": [api_issue(i) for i in sorted(items, key=lambda x: x["created_at"]) if i["status"] == "open"],
            "history": [
                api_issue(i)
                for i in sorted(items, key=lambda x: x.get("closed_at") or "", reverse=True)
                if i["status"] == "closed"
            ],
        })

    # ---- Inventory ----

    def clean_item_fields(data: dict, item: dict) -> str | None:
        """Copy editable fields from ``data`` onto ``item``; returns an error message if invalid."""
        if "name" in data:
            name = str(data["name"]).strip()
            if not name:
                return "Part name is required."
            item["name"] = name
        if "barcode" in data:
            item["barcode"] = str(data["barcode"] or "").strip() or None
        if "bin" in data:
            item["bin"] = str(data["bin"] or "").strip() or None
        for field, cast in (("qty", int), ("min_qty", int), ("unit_cost", float)):
            if field in data:
                try:
                    value = cast(data[field] or 0)
                except (TypeError, ValueError):
                    return f"{field.replace('_', ' ').capitalize()} must be a number."
                if value < 0:
                    return f"{field.replace('_', ' ').capitalize()} can't be negative."
                item[field] = round(value, 2) if cast is float else value
        return None

    @app.get("/api/v1/inventory")
    def api_inventory():
        location, problem = location_for(roles=INVENTORY_ROLES)
        if problem:
            return problem
        items = sorted((i for i in inventory.load() if i["location"] == location), key=lambda i: i["name"].lower())
        return jsonify({"items": items})

    @app.get("/api/v1/inventory/barcode/<path:code>")
    def api_inventory_barcode(code):
        location, problem = location_for(roles=INVENTORY_ROLES)
        if problem:
            return problem
        item = next(
            (i for i in inventory.load() if i["location"] == location and i.get("barcode") == code.strip()),
            None,
        )
        return jsonify(item) if item else error("No part with that barcode.", 404)

    @app.post("/api/v1/inventory")
    def api_create_item():
        location, problem = location_for(roles=INVENTORY_ROLES)
        if problem:
            return problem
        data = body()
        item = {"id": str(uuid.uuid4()), "location": location, "name": "", "barcode": None, "bin": None,
                "qty": 0, "min_qty": 0, "unit_cost": 0.0, "updated_at": _now(), "updated_by": session["user"]}
        message = clean_item_fields({"name": "", **data}, item)
        if message:
            return error(message, 400)
        with _inventory_lock:
            items = inventory.load()
            if item["barcode"] and any(i["location"] == location and i.get("barcode") == item["barcode"] for i in items):
                return error("Another part already uses that barcode.", 400)
            items.append(item)
            inventory.save(items)
        return jsonify(item), 201

    @app.patch("/api/v1/inventory/<item_id>")
    def api_update_item(item_id):
        location, problem = location_for(roles=INVENTORY_ROLES)
        if problem:
            return problem
        data = body()
        with _inventory_lock:
            items = inventory.load()
            item = next((i for i in items if i["id"] == item_id and i["location"] == location), None)
            if not item:
                return error("Part not found.", 404)
            if "delta" in data:  # receive stock (+) or count a loss (-)
                try:
                    delta = int(data["delta"])
                except (TypeError, ValueError):
                    return error("Adjustment must be a whole number.", 400)
                data = {**data, "qty": item["qty"] + delta}
                if data["qty"] < 0:
                    return error("That would take stock below zero.", 400)
            message = clean_item_fields(data, item)
            if message:
                return error(message, 400)
            if item.get("barcode") and any(
                i is not item and i["location"] == location and i.get("barcode") == item["barcode"] for i in items
            ):
                return error("Another part already uses that barcode.", 400)
            item["updated_at"] = _now()
            item["updated_by"] = session["user"]
            inventory.save(items)
        return jsonify(item)

    @app.delete("/api/v1/inventory/<item_id>")
    def api_delete_item(item_id):
        location, problem = location_for(roles=INVENTORY_ROLES)
        if problem:
            return problem
        with _inventory_lock:
            items = inventory.load()
            remaining = [i for i in items if not (i["id"] == item_id and i["location"] == location)]
            if len(remaining) == len(items):
                return error("Part not found.", 404)
            inventory.save(remaining)
        return jsonify({"ok": True})

    # ---- Property settings ----

    def api_property(location: str, record: dict | None) -> dict:
        record = record or {}
        return {
            "location": location,
            "name": record.get("name") or "",
            "address": record.get("address") or "",
            "phone": record.get("phone") or "",
            "rooms": record.get("rooms") or [],
            # A list, not an object keyed by room, so clients never rewrite the keys ("305B").
            "room_types": [{"room": room, "type": code} for room, code in (record.get("room_types") or {}).items()],
            "areas": record.get("areas") or [],
            "pm_items": record.get("pm_items") or DEFAULT_PM_ITEMS,
            "updated_at": record.get("updated_at"),
            "updated_by": record.get("updated_by"),
        }

    @app.get("/api/v1/property")
    def api_get_property():
        """The signed-in user's property. Everyone can read it; the room list feeds the app."""
        location, problem = location_for()
        if problem:
            return problem
        return jsonify(api_property(location, properties.load().get(location)))

    @app.put("/api/v1/property")
    def api_update_property():
        """Supervisors edit their own location: any of name, address, phone, rooms (full list),
        room_types ([{room, type}] to set or clear), areas, pm_items (the quarterly checklist, in order)."""
        location, problem = location_for(roles=PROPERTY_ROLES)
        if problem:
            return problem
        data = body()
        with _property_lock:
            everything = properties.load()
            record = dict(everything.get(location) or {})
            for field, limit in (("name", 120), ("address", 500), ("phone", 40)):
                if field in data:
                    value = str(data[field] or "").strip()
                    if len(value) > limit:
                        return error(f"{field.capitalize()} is too long.", 400)
                    record[field] = value
            if "rooms" in data:
                rooms, message = clean_rooms(data["rooms"])
                if message:
                    return error(message, 400)
                record["rooms"] = rooms
            if "room_types" in data:  # [{"room", "type"}]: sets each listed room; a blank type clears it
                entries = data["room_types"]
                if not isinstance(entries, list):
                    return error("room_types must be a list of {room, type}.", 400)
                types = dict(record.get("room_types") or {})
                room_set = set(record.get("rooms") or [])
                for entry in entries:
                    room = str((entry or {}).get("room", "")).strip().upper()
                    if room not in room_set:
                        return error(f"Room {room or '?'} isn't on the room list.", 400)
                    code, message = clean_room_type((entry or {}).get("type"))
                    if message:
                        return error(message, 400)
                    if code:
                        types[room] = code
                    else:
                        types.pop(room, None)
                record["room_types"] = types
            if "rooms" in data:  # rooms taken off the list lose their type
                record["room_types"] = {r: c for r, c in (record.get("room_types") or {}).items()
                                        if r in set(record["rooms"])}
            if "areas" in data or "rooms" in data:
                areas, message = clean_areas(data.get("areas", record.get("areas") or []), record.get("rooms") or [])
                if message:
                    return error(message, 400)
                record["areas"] = areas
            if "pm_items" in data:
                items, message = clean_items(data["pm_items"])
                if message:
                    return error(message, 400)
                record["pm_items"] = items
            record["updated_at"] = _now()
            record["updated_by"] = session["user"]
            everything[location] = record
            properties.save(everything)
        return jsonify(api_property(location, record))

    # ---- Quarterly PM checklists ----

    def today() -> date:
        return datetime.now(h.TIMEZONE).date()

    def pm_items_for(location: str) -> list[str]:
        return (properties.load().get(location) or {}).get("pm_items") or DEFAULT_PM_ITEMS

    def open_pm_issues(location: str) -> list[dict]:
        return [i for i in h.issues_for_location(location)
                if i["status"] == "open" and h.issue_category(i) == "maintenance"]

    def pm_status(record: dict | None, items: list[str]) -> str:
        if record and record.get("completed"):
            return "done"
        if record and any(item in (record.get("ratings") or {}) for item in items):
            return "in_progress"
        return "due"

    def requested_quarter() -> tuple[int, int] | None:
        """?year=&quarter= or the current quarter; None if invalid or in the future."""
        current = quarter_of(today())
        try:
            year = int(request.args.get("year", current[0]))
            quarter = int(request.args.get("quarter", current[1]))
        except ValueError:
            return None
        if quarter not in (1, 2, 3, 4) or (year, quarter) > current or year < 2000:
            return None
        return year, quarter

    def api_pm_record(record: dict | None) -> dict | None:
        if not record:
            return None
        # A list rather than an object keyed by item name, so clients never rewrite the keys.
        return {
            "ratings": [{"item": item, "rating": rating} for item, rating in (record.get("ratings") or {}).items()],
            "notes": record.get("notes") or "",
            "date": record.get("date"),
            "by": record.get("by"),
            "completed": bool(record.get("completed")),
            "smoke_detector_date": record.get("smoke_detector_date"),
        }

    @app.get("/api/v1/pm")
    def api_pm_quarter():
        """Every listed room's PM status for a quarter (default: this one), plus open PM requests
        for areas or places not on the room list."""
        location, problem = location_for(roles=PM_ROLES)
        if problem:
            return problem
        when = requested_quarter()
        if not when:
            return error("Choose this quarter or an earlier one.", 400)
        year, quarter = when
        items = pm_items_for(location)
        records = {r["room"]: r for r in pm_records.for_location(location)
                   if r["year"] == year and r["quarter"] == quarter}
        prop = properties.load().get(location) or {}
        listed = prop.get("rooms") or []
        room_types = prop.get("room_types") or {}
        listed_keys = {place_key(r) for r in listed}
        open_counts: dict[str, int] = {}
        other_requests = []  # areas, and older requests typed with free text
        for issue in open_pm_issues(location):
            if place_key(issue["room"]) in listed_keys:
                open_counts[issue["room"]] = open_counts.get(issue["room"], 0) + 1
            else:
                other_requests.append(issue)
        # Only guest rooms on the property's room list get quarterly PM.
        rooms = sorted(listed, key=room_sort_key)
        start, end = quarter_bounds(year, quarter)
        result = []
        for room in rooms:
            record = records.get(room)
            result.append({
                "room": room,
                "status": pm_status(record, items),
                "date": record.get("date") if record else None,
                "by": record.get("by") if record else None,
                "open_requests": open_counts.get(room, 0),
                "type": room_types.get(room),
            })
        return jsonify({
            "year": year, "quarter": quarter, "starts": start.isoformat(), "ends": end.isoformat(),
            "items": items, "rooms": result,
            "done": sum(1 for r in result if r["status"] == "done"),
            "other_requests": [api_issue(i) for i in sorted(other_requests, key=lambda i: i["created_at"])],
        })

    @app.get("/api/v1/pm/rooms/<room>")
    def api_pm_room(room):
        """One room's checklist for a year: every quarter's ratings, plus its open PM requests."""
        location, problem = location_for(roles=PM_ROLES)
        if problem:
            return problem
        try:
            year = int(request.args.get("year", quarter_of(today())[0]))
        except ValueError:
            return error("Invalid year.", 400)
        return jsonify(pm_room_detail(location, room.strip().upper(), year))

    def pm_room_detail(location: str, room: str, year: int) -> dict:
        current_year, current_quarter = quarter_of(today())
        quarters = {str(q): None for q in (1, 2, 3, 4)}
        room_records = sorted(
            (r for r in pm_records.for_location(location) if r["room"] == room),
            key=lambda r: (r["year"], r["quarter"]),
        )
        for record in room_records:
            if record["year"] == year:
                quarters[str(record["quarter"])] = api_pm_record(record)
        # The detector's printed date carries over until someone records a new one.
        smoke_dates = [r["smoke_detector_date"] for r in room_records if r.get("smoke_detector_date")]
        return {
            "room": room, "year": year,
            "room_type": ((properties.load().get(location) or {}).get("room_types") or {}).get(room),
            "current_quarter": current_quarter if year == current_year else None,
            "items": pm_items_for(location),
            "quarters": quarters,
            "smoke_detector_date": smoke_dates[-1] if smoke_dates else None,
            "open_requests": [api_issue(i) for i in open_pm_issues(location) if i["room"] == room],
        }

    @app.put("/api/v1/pm/rooms/<room>/<int:year>/<int:quarter>")
    def api_save_pm(room, year, quarter):
        """Save a room's checklist for a quarter:
        {"ratings": [{"item", "rating": 0-3}], "notes": "", "date": "YYYY-MM-DD", "complete": bool,
         "smoke_detector_date": "YYYY-MM-DD",  # date printed on the detector
         "close_request_ids": [...]}  # PM requests finished during this PM
        Completing needs every item rated; the tag date defaults to today."""
        location, problem = location_for(roles=PM_ROLES)
        if problem:
            return problem
        room = room.strip().upper()
        if quarter not in (1, 2, 3, 4) or (year, quarter) > quarter_of(today()):
            return error("Choose this quarter or an earlier one.", 400)
        data = body()
        items = pm_items_for(location)
        raw = data.get("ratings") or {}
        if isinstance(raw, list):  # [{"item": ..., "rating": ...}], as the app sends it
            raw = {str(r.get("item")): r.get("rating") for r in raw if isinstance(r, dict)}
        if not isinstance(raw, dict):
            return error("Ratings must be a list of {item, rating}.", 400)
        ratings = {}
        for item, value in raw.items():
            if item not in items:
                continue  # an item since removed from the checklist
            if value is None:
                continue
            if value not in RATINGS:
                return error(f"'{item}' needs a rating from 0 to 3.", 400)
            ratings[item] = value
        complete = bool(data.get("complete"))
        missing = [item for item in items if item not in ratings]
        if complete and missing:
            return error(f"Rate every item before completing ({len(missing)} left).", 400)
        tag_date = None
        if complete:
            raw_date = str(data.get("date") or "").strip()
            try:
                tag_date = datetime.strptime(raw_date, "%Y-%m-%d").date() if raw_date else today()
            except ValueError:
                return error("Select a valid tag date.", 400)
        notes = str(data.get("notes") or "").strip()[:2000]
        smoke_date = None
        if data.get("smoke_detector_date"):
            try:
                smoke_date = datetime.strptime(str(data["smoke_detector_date"]).strip(), "%Y-%m-%d").date().isoformat()
            except ValueError:
                return error("Select a valid smoke detector date.", 400)
        close_ids = data.get("close_request_ids") or []
        if not isinstance(close_ids, list):
            return error("close_request_ids must be a list.", 400)
        room_issue_ids = {i["id"] for i in open_pm_issues(location) if i["room"] == room}
        if any(issue_id not in room_issue_ids for issue_id in close_ids):
            return error("A PM request to close was not found for this room.", 404)
        if close_ids and session.get("role") not in WORK_ROLES:
            return error("Only maintenance can complete requests.", 403)

        pm_records.upsert(location, room, year, quarter, {
            "ratings": ratings,
            "notes": notes,
            "completed": complete,
            "date": tag_date.isoformat() if tag_date else None,
            "by": session["user"],
            "smoke_detector_date": smoke_date,
        })
        for issue_id in close_ids:
            store.close_issue(issue_id, session["user"], f"Done during Q{quarter} {year} PM.", location)
        return jsonify(pm_room_detail(location, room, year))

    # ---- Users (same rules as the web supervisor page) ----

    MANAGED_ROLES = ("maintenance", "front_desk", "supervisor")

    def api_user(user: dict) -> dict:
        return {
            "username": user["username"],
            "email": user.get("email") or "",
            "role": user["role"],
            "must_change_password": bool(user.get("force_password_change")),
        }

    def managed_users(settings: dict, location: str) -> list[dict]:
        """Non-admin accounts at ``location``; admins are managed only on the web admin page."""
        return [u for u in settings["users"] if u.get("location") == location and u.get("role") != "admin"]

    def load_users_settings() -> dict:
        settings = store.load_settings()
        if h.normalize_users(settings):
            store.save_settings(settings)
        return settings

    @app.get("/api/v1/users")
    def api_users():
        location, problem = location_for(roles=PROPERTY_ROLES)
        if problem:
            return problem
        users = sorted(managed_users(load_users_settings(), location), key=lambda u: u["username"])
        return jsonify({"users": [api_user(u) for u in users]})

    @app.post("/api/v1/users")
    def api_add_user():
        """{"username", "email", "role", "password"}: a temporary password the person changes at first sign-in."""
        location, problem = location_for(roles=PROPERTY_ROLES)
        if problem:
            return problem
        data = body()
        username = str(data.get("username", "")).strip().lower()
        email = str(data.get("email", "")).strip().lower()
        role = str(data.get("role", "")).strip()
        password = str(data.get("password", "")).strip()
        if not username or not email or not password or role not in MANAGED_ROLES:
            return error("Username, email, password, and a valid role are required.", 400)
        if len(password) < 8:
            return error("Password must be at least 8 characters.", 400)
        if not all(c.isalnum() or c in "-_." for c in username):
            return error("Usernames can use letters, numbers, dots, dashes and underscores.", 400)
        settings = load_users_settings()
        if any(u["username"].lower() == username for u in settings["users"]):
            return error("Username already exists.", 400)
        user = {
            "username": username,
            "email": email,
            "password": h.hash_password(password),
            "role": role,
            "location": location,
            "force_password_change": True,
            "reset_token": None,
            "reset_expires_at": None,
        }
        settings["users"].append(user)
        store.save_settings(settings)
        return jsonify(api_user(user)), 201

    def find_managed(settings: dict, location: str, username: str) -> dict | None:
        username = username.strip().lower()
        return next((u for u in managed_users(settings, location) if u["username"].lower() == username), None)

    @app.patch("/api/v1/users/<username>")
    def api_update_user(username):
        """Change role and/or email, or set a temporary password ("password")."""
        location, problem = location_for(roles=PROPERTY_ROLES)
        if problem:
            return problem
        data = body()
        settings = load_users_settings()
        user = find_managed(settings, location, username)
        if not user:
            return error("User was not found at your location.", 404)
        if "role" in data:
            if data["role"] not in MANAGED_ROLES:
                return error("Select a valid role.", 400)
            if user["username"] == session["user"] and data["role"] != user["role"]:
                return error("You can't change your own role.", 400)
            user["role"] = data["role"]
        if "email" in data:
            email = str(data["email"] or "").strip().lower()
            if not email:
                return error("Email is required.", 400)
            user["email"] = email
        if "password" in data:
            password = str(data["password"] or "").strip()
            if len(password) < 8:
                return error("Password must be at least 8 characters.", 400)
            user["password"] = h.hash_password(password)
            user["force_password_change"] = True
        store.save_settings(settings)
        return jsonify(api_user(user))

    @app.delete("/api/v1/users/<username>")
    def api_delete_user(username):
        location, problem = location_for(roles=PROPERTY_ROLES)
        if problem:
            return problem
        settings = load_users_settings()
        user = find_managed(settings, location, username)
        if not user:
            return error("User was not found at your location.", 404)
        if user["username"] == session["user"]:
            return error("You can't delete your own account.", 400)
        settings["users"].remove(user)
        store.save_settings(settings)
        return jsonify({"ok": True})

    # ---- Reports ----

    @app.get("/api/v1/reports/response-times")
    def api_response_times():
        """Averages over jobs closed in the last ``days`` days, per technician, plus open-job aging."""
        location, problem = location_for(roles=REPORT_ROLES)
        if problem:
            return problem
        try:
            days = max(1, min(365, int(request.args.get("days", 30))))
        except ValueError:
            days = 30
        since = datetime.now(timezone.utc) - timedelta(days=days)
        issues = h.issues_for_location(location)
        closed = [i for i in issues if i["status"] == "closed" and (_parse(i.get("closed_at")) or since) >= since]
        open_items = [i for i in issues if i["status"] == "open"]

        def summarize(group: list[dict]) -> dict:
            metrics = [work_metrics(i) for i in group]

            def stat(key, fn):
                values = [m[key] for m in metrics if m[key] is not None]
                return round(fn(values), 1) if values else None

            return {
                "count": len(group),
                "avg_response_minutes": stat("response_minutes", statistics.mean),
                "median_response_minutes": stat("response_minutes", statistics.median),
                "avg_labor_minutes": stat("labor_minutes", statistics.mean),
                "avg_parts_wait_minutes": stat("parts_wait_minutes", statistics.mean),
                "avg_resolution_minutes": stat("resolution_minutes", statistics.mean),
                "parts_cost": round(sum(p["qty"] * p["unit_cost"] for i in group for p in i.get("parts", [])), 2),
            }

        by_tech: dict[str, list[dict]] = {}
        for issue in closed:
            by_tech.setdefault(issue.get("closed_by") or "unknown", []).append(issue)

        now = datetime.now(timezone.utc)
        oldest_open = sorted(open_items, key=lambda i: i["created_at"])[:10]
        return jsonify({
            "days": days,
            "overall": summarize(closed),
            "by_category": {c: summarize([i for i in closed if h.issue_category(i) == c]) for c in h.ISSUE_CATEGORIES},
            "by_technician": sorted(
                ({"name": name, **summarize(group)} for name, group in by_tech.items()),
                key=lambda row: -row["count"],
            ),
            "open": {
                "count": len(open_items),
                "awaiting_parts": sum(1 for i in open_items if work_state(i) == "awaiting_parts"),
                "not_started": sum(1 for i in open_items if work_state(i) == "new"),
                "oldest": [
                    {**api_issue(i), "age_minutes": round((now - _parse(i["created_at"])).total_seconds() / 60, 1)}
                    for i in oldest_open
                ],
            },
        })
