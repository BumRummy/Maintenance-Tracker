"""Enrolled kiosk phones: each shared work phone gets its own key when it is set up, so its SOS
key reaches the server even when nobody is signed in on it.

How a phone gets its key:
  - Zero-touch or QR setup: a supervisor creates an enrollment code for the property; it goes in
    the setup configuration, and the phone trades it for a device key during first boot.
  - USB setup: a supervisor signs in on the phone and taps "Enroll this phone".

A device key can only raise an emergency alert, read the room-beacon map and check in (its
identifiers, battery and app version). IMEI, ICCID and serial number are shown to supervisors and
admins only. Keys and codes are
stored as SHA-256 hashes; the plain value is shown once. The device list doubles as the panic
button record a property keeps (which phones, enrolled when, last heard from).

Stored in kiosk_devices.json: {"codes": {id: {...}}, "devices": {id: {...}}}.
"""

import hashlib
import json
import os
import secrets
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

CODE_PREFIX = "hce_"
KEY_PREFIX = "hck_"
MAX_NAME = 40
SEEN_WRITE_EVERY = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def clean_name(raw) -> str | None:
    name = " ".join(str(raw or "").split())[:MAX_NAME]
    return name or None


IDENTIFIER_FIELDS = ("imei", "iccid", "serial")
INFO_FIELDS = ("model", "android", "app_version")


def clean_digits(raw, longest: int) -> str | None:
    """IMEI (15-16 digits) or ICCID (19-20 digits, may end in F): digits/hex only, no spaces."""
    value = "".join(c for c in str(raw or "").upper() if c.isalnum())
    return value[:longest] if value and all(c in "0123456789ABCDEF" for c in value) else None


def clean_phone_info(raw) -> dict:
    """What a phone reports about itself. Unknown keys are ignored; bad values become None."""
    raw = raw if isinstance(raw, dict) else {}
    info = {
        "imei": clean_digits(raw.get("imei"), 17),
        "iccid": clean_digits(raw.get("iccid"), 22),
        "serial": clean_info(raw.get("serial")),
        "model": clean_info(raw.get("model")),
        "android": clean_info(raw.get("android")),
        "app_version": clean_info(raw.get("app_version")),
    }
    battery = raw.get("battery")
    if isinstance(battery, int) and not isinstance(battery, bool) and 0 <= battery <= 100:
        info["battery"] = battery
    return info


def clean_info(raw) -> str | None:
    """Serial number or model: short printable text only."""
    value = "".join(c for c in str(raw or "") if c.isprintable()).strip()[:60]
    return value or None


class KioskDevices:
    def __init__(self, config_dir: str):
        self.path = Path(config_dir) / "kiosk_devices.json"
        self.lock = threading.Lock()

    def load(self) -> dict:
        if not self.path.exists():
            return {"codes": {}, "devices": {}}
        with self.path.open(encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("codes", {})
        data.setdefault("devices", {})
        return data

    def _save(self, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".kiosk_devices.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    # ---- Enrollment codes (one per property or batch of phones; reusable until revoked) ----

    def create_code(self, location: str, label: str | None, username: str) -> tuple[dict, str]:
        code = CODE_PREFIX + secrets.token_urlsafe(24)
        record = {"id": uuid.uuid4().hex[:12], "location": location, "label": label, "hash": _hash(code),
                  "created_by": username, "created_at": _now().isoformat(), "revoked_at": None, "uses": 0}
        with self.lock:
            data = self.load()
            data["codes"][record["id"]] = record
            self._save(data)
        return record, code

    def codes(self, location: str) -> list[dict]:
        return sorted((c for c in self.load()["codes"].values() if c["location"] == location),
                      key=lambda c: c["created_at"], reverse=True)

    def revoke_code(self, location: str, code_id: str) -> bool:
        with self.lock:
            data = self.load()
            record = data["codes"].get(code_id)
            if not record or record["location"] != location:
                return False
            record["revoked_at"] = record["revoked_at"] or _now().isoformat()
            self._save(data)
            return True

    # ---- Devices ----

    def _new_device(self, data: dict, location: str, name: str, via: str, by: str | None,
                    serial: str | None, model: str | None) -> tuple[dict, str]:
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        device = {"id": uuid.uuid4().hex[:12], "location": location, "name": name, "key_hash": _hash(key),
                  "serial": serial, "model": model, "enrolled_at": _now().isoformat(), "enrolled_via": via,
                  "enrolled_by": by, "last_seen_at": None, "revoked_at": None}
        data["devices"][device["id"]] = device
        return device, key

    def enroll_with_code(self, code: str, name: str | None, serial: str | None, model: str | None) -> tuple[dict, str] | None:
        """A phone in setup trades an enrollment code for its own key. None if the code is wrong or revoked."""
        digest = _hash(str(code or ""))
        with self.lock:
            data = self.load()
            record = next((c for c in data["codes"].values()
                           if secrets.compare_digest(c["hash"], digest) and not c["revoked_at"]), None)
            if record is None:
                return None
            record["uses"] = record.get("uses", 0) + 1
            name = name or f"Kiosk phone {record['uses']}"
            device, key = self._new_device(data, record["location"], name, "code:" + record["id"], record["created_by"], serial, model)
            self._save(data)
            return device, key

    def enroll_direct(self, location: str, name: str, username: str, serial: str | None, model: str | None) -> tuple[dict, str]:
        """A supervisor signed in on the phone enrolls it."""
        with self.lock:
            data = self.load()
            device, key = self._new_device(data, location, name, "supervisor", username, serial, model)
            self._save(data)
            return device, key

    def authenticate(self, key: str | None) -> dict | None:
        """The enrolled, unrevoked device with this key; notes when it was last heard from."""
        if not key or not key.startswith(KEY_PREFIX):
            return None
        digest = _hash(key)
        with self.lock:
            data = self.load()
            device = next((d for d in data["devices"].values()
                           if secrets.compare_digest(d["key_hash"], digest) and not d["revoked_at"]), None)
            if device is None:
                return None
            last = device.get("last_seen_at")
            if not last or _now() - datetime.fromisoformat(last) > SEEN_WRITE_EVERY:
                device["last_seen_at"] = _now().isoformat()
                self._save(data)
            return device

    def devices(self, location: str) -> list[dict]:
        return sorted((d for d in self.load()["devices"].values() if d["location"] == location),
                      key=lambda d: d["enrolled_at"])

    def check_in(self, device_id: str, info: dict) -> dict | None:
        """The phone reports its identifiers, battery and versions (at enrollment and every few minutes).
        A value the phone can't read this time (no SIM yet, say) doesn't erase the one we had."""
        with self.lock:
            data = self.load()
            device = data["devices"].get(device_id)
            if not device or device["revoked_at"]:
                return None
            for key, value in info.items():
                if value is not None:
                    device[key] = value
            device["checked_in_at"] = device["last_seen_at"] = _now().isoformat()
            self._save(data)
            return device

    def update(self, location: str, device_id: str, name: str | None = None, revoke: bool = False) -> dict | None:
        with self.lock:
            data = self.load()
            device = data["devices"].get(device_id)
            if not device or device["location"] != location:
                return None
            if name:
                device["name"] = name
            if revoke:
                device["revoked_at"] = device["revoked_at"] or _now().isoformat()
            self._save(data)
            return device


def public_device(device: dict) -> dict:
    """Safe for the phone itself and anyone at the property: no hardware identifiers."""
    return {k: device.get(k) for k in ("id", "name", "model", "enrolled_at", "enrolled_via",
                                       "enrolled_by", "last_seen_at", "revoked_at")}


def admin_device(device: dict) -> dict:
    """Supervisors and admins: the device record including IMEI, ICCID and serial number."""
    return public_device(device) | {k: device.get(k) for k in
                                    IDENTIFIER_FIELDS + ("android", "app_version", "battery", "checked_in_at")}


def public_code(code: dict) -> dict:
    return {k: code.get(k) for k in ("id", "label", "created_by", "created_at", "revoked_at", "uses")}
