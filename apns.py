"""Apple Push Notification service (APNs) for the HotelCraft iPhone app.

Configured with environment variables from an APNs auth key (Apple Developer →
Certificates, IDs & Profiles → Keys → "Apple Push Notifications service"):

    APNS_AUTH_KEY   contents of the downloaded AuthKey_XXXXXXXXXX.p8 file
    APNS_KEY_ID     the 10-character Key ID
    APNS_TEAM_ID    the Apple developer Team ID
    APNS_TOPIC      the app's bundle ID (default Com.HotelCraft.Maintenance)

Without them, app notifications are skipped and everything else works as before.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jwt

PRODUCTION_HOST = "https://api.push.apple.com"
SANDBOX_HOST = "https://api.sandbox.push.apple.com"
TOKEN_LIFETIME = 45 * 60  # Apple accepts a provider token for up to an hour.
# APNs reasons meaning the device token will never work again.
DEAD_TOKEN_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}


class APNsSender:
    def __init__(self, auth_key: str, key_id: str, team_id: str, topic: str):
        self.auth_key = auth_key
        self.key_id = key_id
        self.team_id = team_id
        self.topic = topic
        self._token = None
        self._token_at = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "APNsSender | None":
        key = os.getenv("APNS_AUTH_KEY", "").strip().replace("\\n", "\n")
        key_id = os.getenv("APNS_KEY_ID", "").strip()
        team_id = os.getenv("APNS_TEAM_ID", "").strip()
        if not (key and key_id and team_id):
            return None
        return cls(key, key_id, team_id, os.getenv("APNS_TOPIC", "Com.HotelCraft.Maintenance").strip())

    def provider_token(self) -> str:
        with self._lock:
            if not self._token or time.time() - self._token_at > TOKEN_LIFETIME:
                self._token_at = time.time()
                self._token = jwt.encode(
                    {"iss": self.team_id, "iat": int(self._token_at)},
                    self.auth_key,
                    algorithm="ES256",
                    headers={"kid": self.key_id},
                )
            return self._token

    def send(self, client: httpx.Client, device_token: str, payload: dict, sandbox: bool) -> tuple[int, str]:
        """Returns (HTTP status, APNs reason or "")."""
        response = client.post(
            f"{SANDBOX_HOST if sandbox else PRODUCTION_HOST}/3/device/{device_token}",
            content=json.dumps(payload),
            headers={
                "authorization": f"bearer {self.provider_token()}",
                "apns-topic": self.topic,
                "apns-push-type": "alert",
                "apns-priority": "10",
            },
        )
        reason = ""
        if response.status_code != 200:
            try:
                reason = response.json().get("reason", "")
            except ValueError:
                pass
        return response.status_code, reason


class Devices:
    """iPhones registered for notifications, in devices.json: one entry per device token,
    tagged with whoever is signed in on it."""

    def __init__(self, config_dir: str):
        self.path = Path(config_dir) / "devices.json"
        self.lock = threading.Lock()

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            with self.path.open(encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return []

    def _save(self, devices: list[dict]) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(devices, f, indent=2)

    def register(self, token: str, username: str, role: str, location: str, sandbox: bool) -> None:
        with self.lock:
            # A phone that changes hands keeps its token; the latest sign-in owns it.
            devices = [d for d in self.load() if d["token"] != token]
            devices.append({
                "token": token, "username": username, "role": role, "location": location,
                "sandbox": sandbox, "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            self._save(devices)

    def remove(self, tokens: set[str]) -> None:
        if not tokens:
            return
        with self.lock:
            devices = self.load()
            remaining = [d for d in devices if d["token"] not in tokens]
            if len(remaining) != len(devices):
                self._save(remaining)


def new_issue_payload(issue: dict) -> dict:
    urgent = bool(issue.get("urgent"))
    title = f"{'URGENT · ' if urgent else 'New job · '}Room {issue['room']}"
    return {
        "aps": {
            "alert": {"title": title, "body": issue["description"]},
            "sound": "default",
            "thread-id": f"room-{issue['room']}",
        },
        "issue_id": issue["id"],
    }
