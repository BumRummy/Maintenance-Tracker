"""Room location beacons (Bluetooth LE), one per room or area.

Phones scanning in the background report which beacons they hear. That keeps each beacon's
status current (last heard, signal, battery) and lets an emergency alert say where the person
is ("Room 214"). People with beacon access register, edit and monitor beacons.

Stored in beacons.json: {location: {beacon_id: {place, label, registered_by, registered_at,
last_seen_at, last_seen_by, rssi, battery}}}. Beacon IDs are what the phone reads from the
advertisement: an iBeacon "UUID:major:minor", an Eddystone UID, or the beacon's address.
"""

import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_ID = 64
MISSING_AFTER = timedelta(hours=24)  # a room beacon nobody has heard in a day needs checking
LOW_BATTERY = 20  # percent
SIGHTING_WRITE_EVERY = timedelta(minutes=1)
MAX_SIGHTINGS = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clean_id(raw) -> str | None:
    value = str(raw or "").strip().upper()
    if not value or len(value) > MAX_ID or not all(c.isalnum() or c in ":-" for c in value):
        return None
    return value


def beacon_status(record: dict, now: datetime | None = None) -> str:
    """never | missing | low_battery | ok"""
    now = now or _now()
    seen = record.get("last_seen_at")
    if not seen:
        return "never"
    if now - datetime.fromisoformat(seen) > MISSING_AFTER:
        return "missing"
    battery = record.get("battery")
    if isinstance(battery, int) and battery <= LOW_BATTERY:
        return "low_battery"
    return "ok"


class Beacons:
    def __init__(self, config_dir: str):
        self.path = Path(config_dir) / "beacons.json"
        self.lock = threading.Lock()

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        with self.path.open(encoding="utf-8") as f:
            return json.load(f)

    def _save(self, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".beacons.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def for_location(self, location: str) -> dict:
        return self.load().get(location, {})

    def place_for(self, location: str, beacon_id: str | None) -> str | None:
        beacon_id = clean_id(beacon_id)
        return self.for_location(location).get(beacon_id, {}).get("place") if beacon_id else None

    def put(self, location: str, beacon_id: str, place: str, label: str | None, username: str) -> tuple[dict, str | None]:
        """Register or edit. Returns (record, error). One beacon per room or area."""
        with self.lock:
            data = self.load()
            beacons = data.setdefault(location, {})
            other = next((bid for bid, b in beacons.items() if b.get("place") == place and bid != beacon_id), None)
            if other:
                return {}, f"{place} already has beacon {other}. Remove that one first."
            record = beacons.get(beacon_id) or {"registered_by": username, "registered_at": _now().isoformat(),
                                                "last_seen_at": None, "last_seen_by": None, "rssi": None, "battery": None}
            record.update(place=place, label=label, updated_by=username, updated_at=_now().isoformat())
            beacons[beacon_id] = record
            self._save(data)
            return record, None

    def remove(self, location: str, beacon_id: str) -> bool:
        with self.lock:
            data = self.load()
            if beacon_id not in data.get(location, {}):
                return False
            del data[location][beacon_id]
            self._save(data)
            return True

    def sightings(self, location: str, username: str, seen: list[dict]) -> int:
        """Record beacons a phone heard. Unregistered beacons are ignored. Returns how many matched."""
        now = _now()
        with self.lock:
            data = self.load()
            beacons = data.get(location, {})
            matched, changed = 0, False
            for s in seen:
                record = beacons.get(s["id"])
                if record is None:
                    continue
                matched += 1
                last = record.get("last_seen_at")
                if last and now - datetime.fromisoformat(last) < SIGHTING_WRITE_EVERY and s.get("battery") is None:
                    continue
                record.update(last_seen_at=now.isoformat(), last_seen_by=username, rssi=s.get("rssi"))
                if s.get("battery") is not None:
                    record["battery"] = s["battery"]
                changed = True
            if changed:
                self._save(data)
            return matched
