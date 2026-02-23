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
