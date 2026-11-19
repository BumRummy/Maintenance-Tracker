# Maintenance Tracker (Docker Web Host)

This project runs as a web-hosted app in Docker with:

- Forum login for non-admin users.
- Admin login for configuration.
- Admin settings page to manage forum users, create properties, assign users to properties, and configure per-property delivery/reporting settings.
- Environment variable support for admin login, web host/port, `PUID`, `PGID`, and `/config` location.

## Run with Docker Compose

```bash
docker compose up --build
```

## Environment Variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `WEB_HOST` | Bind address for Flask | `0.0.0.0` |
| `WEB_PORT` | Web port (not editable in settings page) | `7070` |
| `ADMIN_USERNAME` | Admin login username (not editable in settings page) | `admin` |
| `ADMIN_PASSWORD` | Admin login password (not editable in settings page) | `admin123` |
| `SECRET_KEY` | Flask session secret key | `change-me` |
| `CONFIG_PATH` | Config directory inside container | `/config` |
| `RESEND_API_KEY` | API key used to send password-reset emails through Resend | *(empty)* |
| `RESEND_FROM` | Sender for password-reset emails | `noreply@bmiMaintenance.com` |
| `TZ` | IANA timezone used to display issue timestamps (for example, `America/Detroit`) | `UTC` |
| `VAPID_PUBLIC_KEY` | URL-safe public VAPID key used by browsers to subscribe to job notifications | Automatically generated |
| `VAPID_PRIVATE_KEY` | Matching private VAPID key used by the server to send job notifications | Automatically generated |
| `VAPID_CLAIMS_EMAIL` | Contact URI included with push requests (normally `mailto:you@example.com`) | `mailto:admin@example.com` |
| `APNS_AUTH_KEY` | Contents of the Apple Push Notifications `.p8` auth key, for HotelCraft iPhone app notifications | *(empty: app notifications off)* |
| `APNS_KEY_ID` | Key ID of that auth key | *(empty)* |
| `APNS_TEAM_ID` | Apple developer Team ID | *(empty)* |
| `APNS_TOPIC` | iPhone app bundle ID | `Com.HotelCraft.Maintenance` |
| `FCM_SERVICE_ACCOUNT` | Whole JSON of a Firebase service account key (Firebase console → Project settings → Service accounts), for HotelCraft Android app notifications | *(empty: Android notifications off)* |
| `SUPPORT_EMAIL` | Contact address shown on the public `/support` and `/privacy` pages | *(empty)* |
| `CONFIG_LOCATION` | Host path mounted to `CONFIG_PATH` | `./config` |
| `PUID` | Desired user id ownership for config directory | `1000` |
| `PGID` | Desired group id ownership for config directory | `1000` |

## URLs

- Forum login: `http://localhost:7070/forum/login`
- Admin login: `http://localhost:7070/admin/login`
- Settings page: `http://localhost:7070/settings`

## Notes

- Settings are stored in `${CONFIG_PATH}/settings.json`.
- Each property has its own settings for receiving addresses, SMTP, weekly report delivery, and job completion CC list.
- Admin credentials and web host/port are intentionally environment-controlled.
- A VAPID key pair is generated automatically in the persistent config directory
  when both VAPID variables are not supplied. You can instead configure both
  variables explicitly. Maintenance users can
  then install the site to their iOS or Android home screen and tap **Enable
  notifications**. Push notifications require the deployed site to use HTTPS;
  on iOS, web push requires iOS/iPadOS 16.4 or newer and a home-screen installation.

## Staff emergency alerts and room beacons

- `POST /api/v1/emergency` (any staff, from the phone's SOS key pressed 3 times) raises an alert.
  Every signed-in phone at the property gets it, plus supervisors and admins wherever they are, as a
  high-priority push (data-only on Android, so the app shows a full-screen alarm). The phone that
  raised it is skipped. `GET` lists active alerts (apps check it on refresh in case a push was
  missed); `…/respond` records "on my way"; `…/resolve` (the person who raised it, or a supervisor)
  ends it and notifies everyone. `trigger: "test"` notifies nobody. Every alert, drills included,
  is kept in `${CONFIG_PATH}/emergencies.json`. Supervisors download it from
  `GET /api/v1/emergency/log.csv`, the usage record Washington L&I can request (HB 1524, effective
  2026-01-01).
- Built toward Washington's hotel panic-button requirement (believed to be RCW 49.60.515).
  Each property must confirm its own compliance.
- Room beacons (`/api/v1/beacons`): one Bluetooth beacon per room or area. Supervisors, admins and
  users granted `beacon_access` (`PATCH /api/v1/users/<name>`) register, edit and monitor them.
  Status is `ok`, `low_battery` (≤ 20 %), `missing` (not heard in 24 h) or `never`. Phones report
  what they hear to `/api/v1/beacons/sightings`; an alert sent with `beacon_id` names the
  exact room ("Room 214") or area ("Lobby") of the strongest beacon the phone heard.
