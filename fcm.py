"""Firebase Cloud Messaging (HTTP v1) for the HotelCraft Android app.

Configured with one environment variable holding a Firebase service account key
(Firebase console → Project settings → Service accounts → Generate new private key):

    FCM_SERVICE_ACCOUNT   the whole downloaded JSON file's contents

Without it, Android notifications are skipped and everything else works as before.
"""

import json
import os
import threading
import time

import httpx
import jwt

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TOKEN_LIFETIME = 50 * 60  # Google access tokens last an hour.
# FCM error codes meaning the registration token will never work again.
DEAD_TOKEN_ERRORS = {"UNREGISTERED", "INVALID_ARGUMENT"}
ANDROID_CHANNEL = "jobs_v2"  # must match the channel the app creates (Push.CHANNEL_JOBS)


class FCMSender:
    def __init__(self, service_account: dict):
        self.project_id = service_account["project_id"]
        self.client_email = service_account["client_email"]
        self.private_key = service_account["private_key"]
        self.token_uri = service_account.get("token_uri", "https://oauth2.googleapis.com/token")
        self._token = None
        self._token_at = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "FCMSender | None":
        raw = os.getenv("FCM_SERVICE_ACCOUNT", "").strip()
        if not raw:
            return None
        try:
            account = json.loads(raw)
            return cls(account)
        except (ValueError, KeyError, TypeError):
            return None

    def assertion(self, now: int | None = None) -> str:
        """The signed JWT exchanged for an access token (OAuth 2.0 service-account flow)."""
        now = int(now or time.time())
        return jwt.encode(
            {"iss": self.client_email, "scope": SCOPE, "aud": self.token_uri, "iat": now, "exp": now + 3600},
            self.private_key,
            algorithm="RS256",
        )

    def access_token(self, client: httpx.Client) -> str:
        with self._lock:
            if not self._token or time.time() - self._token_at > TOKEN_LIFETIME:
                response = client.post(self.token_uri, data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": self.assertion(),
                })
                response.raise_for_status()
                self._token = response.json()["access_token"]
                self._token_at = time.time()
            return self._token

    def send(self, client: httpx.Client, device_token: str, message: dict) -> tuple[int, str]:
        """Returns (HTTP status, FCM error code or "")."""
        response = client.post(
            f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send",
            json={"message": {**message, "token": device_token}},
            headers={"authorization": f"Bearer {self.access_token(client)}"},
        )
        code = ""
        if response.status_code != 200:
            try:
                error = response.json().get("error", {})
                code = next((d.get("errorCode") for d in error.get("details", []) if d.get("errorCode")), "") \
                    or error.get("status", "")
            except ValueError:
                pass
        return response.status_code, code


def new_issue_message(issue: dict) -> dict:
    urgent = bool(issue.get("urgent"))
    title = f"{'URGENT · ' if urgent else 'New job · '}Room {issue['room']}"
    return {
        "notification": {"title": title, "body": issue["description"]},
        "data": {"issue_id": issue["id"], "room": issue["room"]},
        "android": {
            "priority": "high",
            "notification": {"channel_id": ANDROID_CHANNEL, "tag": f"room-{issue['room']}", "sound": "default"},
        },
    }
