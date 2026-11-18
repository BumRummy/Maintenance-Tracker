"""Live room occupancy and housekeeping status from the HotelCraft Bridge.

The bridge is a small box at the hotel that holds the PMS interface (FIAS) link and
posts room changes here over HTTPS. It authenticates with a per-location bearer token,
never a user session, and it can only write room status for its own location.

Nothing about guests is stored: only room number, occupied yes/no, the housekeeping
status and when it changed.

Files (next to the other data files):
    bridge_tokens.json   [{id, location, name, token_hash, created_*, last_seen_at, ...}]
    room_status.json     {location: {room: {occupied, status, rs_code, at, updated_at, source}}}
"""

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

TOKEN_PREFIX = "hcb_"
# Housekeeping statuses the bridge may report, after it maps the hotel's RS codes.
STATUSES = ("dirty", "clean", "inspected", "pickup", "out_of_order", "out_of_service")
SOURCES = ("GI", "GO", "GC", "RE", "DS", "manual")
MAX_EVENTS = 3000
MAX_TOKENS_PER_LOCATION = 5
BRIDGE_ONLINE_WINDOW = timedelta(minutes=5)

_lock = Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(token: str) -> str:
    # Tokens are 256 random bits, so a fast hash is enough (no password stretching needed).
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class _JSONFile:
    def __init__(self, path: Path, empty):
        self.path = path
        self.empty = empty

    def load(self):
        if not self.path.exists():
            return self.empty()
        with self.path.open(encoding="utf-8") as f:
            return json.load(f)

    def save(self, data) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


class BridgeTokens:
    def __init__(self, config_dir: str):
        self.file = _JSONFile(Path(config_dir) / "bridge_tokens.json", list)

    def for_location(self, location: str) -> list[dict]:
        return [t for t in self.file.load() if t.get("location") == location]

    def create(self, location: str, name: str, created_by: str) -> tuple[dict | None, str | None, str | None]:
        """Returns (record, token, error). The token itself is only ever returned here."""
        with _lock:
            tokens = self.file.load()
            if sum(1 for t in tokens if t.get("location") == location) >= MAX_TOKENS_PER_LOCATION:
                return None, None, f"A property can have at most {MAX_TOKENS_PER_LOCATION} bridge tokens."
            token = TOKEN_PREFIX + secrets.token_urlsafe(32)
            record = {
                "id": uuid.uuid4().hex[:12],
                "location": location,
                "name": name,
                "token_hash": _hash(token),
                "created_at": _now(),
                "created_by": created_by,
                "last_seen_at": None,
                "pms_link": None,
                "version": None,
                "last_resync_at": None,
            }
            tokens.append(record)
            self.file.save(tokens)
        return record, token, None

    def revoke(self, location: str, token_id: str) -> bool:
        with _lock:
            tokens = self.file.load()
            kept = [t for t in tokens if not (t["id"] == token_id and t.get("location") == location)]
            if len(kept) == len(tokens):
                return False
            self.file.save(kept)
        return True

    def authenticate(self, header: str | None) -> dict | None:
        if not header or not header.startswith("Bearer "):
            return None
        token = header[len("Bearer "):].strip()
        if not token.startswith(TOKEN_PREFIX):
            return None
        digest = _hash(token)
        for record in self.file.load():
            if hmac.compare_digest(record.get("token_hash", ""), digest):
                return record
        return None

    def touch(self, token_id: str, **fields) -> None:
        with _lock:
            tokens = self.file.load()
            for record in tokens:
                if record["id"] == token_id:
                    record["last_seen_at"] = _now()
                    record.update({k: v for k, v in fields.items() if v is not None})
            self.file.save(tokens)


def public_token(record: dict) -> dict:
    return {k: record.get(k) for k in
            ("id", "name", "created_at", "created_by", "last_seen_at", "pms_link", "version", "last_resync_at")}


def bridge_summary(tokens: list[dict]) -> dict:
    """Is any bridge at this location reporting? Used by the app to label the vacant list."""
    seen = [t for t in tokens if t.get("last_seen_at")]
    if not seen:
        return {"configured": bool(tokens), "online": False, "last_seen_at": None, "pms_link": None}
    latest = max(seen, key=lambda t: t["last_seen_at"])
    last = datetime.fromisoformat(latest["last_seen_at"])
    online = datetime.now(timezone.utc) - last <= BRIDGE_ONLINE_WINDOW
    return {
        "configured": True,
        "online": online,
        "last_seen_at": latest["last_seen_at"],
        "pms_link": latest.get("pms_link") if online else None,
    }


def clean_event(raw, max_room_length: int) -> tuple[dict | None, str | None]:
    if not isinstance(raw, dict):
        return None, "Each event must be an object."
    room = str(raw.get("room") or "").strip().upper()
    if not room or len(room) > max_room_length or not all(c.isalnum() or c == "-" for c in room):
        return None, f"'{room}' isn't a valid room number."
    event = {"room": room}
    if "occupied" in raw:
        if not isinstance(raw["occupied"], bool):
            return None, f"Room {room}: occupied must be true or false."
        event["occupied"] = raw["occupied"]
    if "status" in raw:
        status = raw["status"]
        if status is not None and status not in STATUSES:
            return None, f"Room {room}: unknown status '{status}'."
        event["status"] = status
    if "rs_code" in raw:
        code = raw["rs_code"]
        if code is not None and (not isinstance(code, (str, int)) or len(str(code)) > 4):
            return None, f"Room {room}: bad rs_code."
        event["rs_code"] = None if code is None else str(code)
    source = raw.get("source", "manual")
    if source not in SOURCES:
        return None, f"Room {room}: unknown source '{source}'."
    event["source"] = source
    at = raw.get("at")
    if at is not None:
        if not isinstance(at, str) or len(at) > 40:
            return None, f"Room {room}: bad time."
        event["at"] = at
    if not {"occupied", "status", "rs_code"} & event.keys():
        return None, f"Room {room}: nothing to update."
    return event, None


class RoomStatus:
    def __init__(self, config_dir: str):
        self.file = _JSONFile(Path(config_dir) / "room_status.json", dict)

    def for_location(self, location: str) -> dict:
        return self.file.load().get(location, {})

    def apply(self, location: str, events: list[dict], snapshot: bool = False) -> int:
        """Apply cleaned events in order. A snapshot (after a PMS resync) replaces the
        location's rooms so rooms the PMS no longer lists as occupied don't stay stuck."""
        received = _now()
        with _lock:
            everything = self.file.load()
            rooms = {} if snapshot else dict(everything.get(location, {}))
            for event in events:
                previous = rooms.get(event["room"]) or {}
                record = dict(previous)
                if snapshot and "occupied" not in event:
                    record["occupied"] = False  # resync lists every occupied room
                for key in ("occupied", "status", "rs_code"):
                    if key in event:
                        record[key] = event[key]
                record["source"] = event["source"]
                record["at"] = event.get("at")
                record["updated_at"] = received
                rooms[event["room"]] = record
            everything[location] = rooms
            self.file.save(everything)
        return len(events)
