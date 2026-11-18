"""Staff emergency (panic) alerts.

An employee presses the phone's SOS key three times; the app calls POST /api/v1/emergency
and every staff phone at the property, plus supervisors and admins, gets an alarm. Others
answer "On my way" and a supervisor (or the person who raised it) resolves it.

Built for Washington's hotel panic-button requirement (believed to be RCW 49.60.515: a device
that summons immediate on-scene assistance). The property must confirm its own compliance;
this is one part of it, not a legal guarantee.

Stored in emergencies.json: [{id, location, raised_by, raised_at, near, trigger, status,
responders: [{username, at}], resolved_by, resolved_at, note}].
"""

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

TRIGGERS = ("sos_key", "button", "test")
MAX_NEAR = 60
KEEP_RESOLVED = 500  # history kept for review


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Emergencies:
    def __init__(self, config_dir: str):
        self.path = Path(config_dir) / "emergencies.json"
        self.lock = threading.Lock()

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return json.load(f)

    def _save(self, alerts: list[dict]) -> None:
        active = [a for a in alerts if a["status"] == "active"]
        resolved = [a for a in alerts if a["status"] != "active"][-KEEP_RESOLVED:]
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".emergencies.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(resolved + active, f, indent=2)
        os.replace(tmp, self.path)

    def active(self, location: str) -> list[dict]:
        return [a for a in self.load() if a["location"] == location and a["status"] == "active"]

    def raise_alert(self, location: str, username: str, near: str | None, trigger: str) -> tuple[dict, bool]:
        """Returns (alert, created). Pressing again while your alert is active doesn't
        start a second one; it updates where you are."""
        with self.lock:
            alerts = self.load()
            existing = next((a for a in alerts if a["location"] == location and a["status"] == "active"
                             and a["raised_by"] == username), None)
            if existing:
                if near:
                    existing["near"] = near
                existing["repeated_at"] = _now()
                self._save(alerts)
                return existing, False
            alert = {
                "id": uuid.uuid4().hex[:12], "location": location, "raised_by": username, "raised_at": _now(),
                "near": near, "trigger": trigger, "status": "active", "responders": [],
                "resolved_by": None, "resolved_at": None, "note": None,
            }
            alerts.append(alert)
            self._save(alerts)
            return alert, True

    def update(self, location: str, alert_id: str, change) -> dict | None:
        with self.lock:
            alerts = self.load()
            alert = next((a for a in alerts if a["id"] == alert_id and a["location"] == location), None)
            if alert is None:
                return None
            change(alert)
            self._save(alerts)
            return alert


def alert_title(alert: dict) -> str:
    who = alert["raised_by"].capitalize()
    return f"🚨 EMERGENCY · {who}" + (f" · {alert['near']}" if alert.get("near") else "")


def apns_payload(alert: dict, kind: str) -> dict:
    if kind == "raised":
        alert_text = {"title": alert_title(alert), "body": "Staff member needs help now. Open HotelCraft to respond."}
        sound, level = "default", "time-sensitive"
    else:
        alert_text = {"title": "Emergency resolved", "body": f"{alert['raised_by'].capitalize()}'s alert was resolved."}
        sound, level = "default", "active"
    return {
        "aps": {"alert": alert_text, "sound": sound, "interruption-level": level,
                "thread-id": f"emergency-{alert['id']}"},
        "emergency_id": alert["id"], "emergency": kind,
    }


def fcm_message(alert: dict, kind: str) -> dict:
    """Data-only, high priority: the Android app builds the full-screen alarm itself,
    even when it's in the background."""
    return {
        "data": {
            "type": "emergency", "emergency": kind, "emergency_id": alert["id"],
            "raised_by": alert["raised_by"], "near": alert.get("near") or "", "raised_at": alert["raised_at"],
        },
        "android": {"priority": "high", "ttl": "300s"},
    }
