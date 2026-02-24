# Maintenance Tracker (Docker Web Host)

This project runs as a web-hosted app in Docker with:

- Front desk login for creating new maintenance requests with a simple Location + Issue form.
- Front desk dashboard showing currently open jobs.
- Maintenance login for working/closing jobs and reviewing logs from the last 2 weeks.
- Admin login for managing maintenance users, front desk users, and properties.
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
| `JOBS_FILE` | Optional explicit jobs storage file path | `${CONFIG_PATH}/jobs.json` |
| `PUID` | Desired user id ownership for config directory | `1000` |
| `PGID` | Desired group id ownership for config directory | `1000` |

## URLs

- Front desk login: `http://localhost:7070/frontdesk/login`
- Maintenance login: `http://localhost:7070/forum/login`
- Admin login: `http://localhost:7070/admin/login`
- Settings page: `http://localhost:7070/settings`

## Notes

- Settings are stored in `${CONFIG_PATH}/settings.json`.
- Jobs are stored in `${JOBS_FILE}` (defaults to `${CONFIG_PATH}/jobs.json`).
- Completed jobs store the username of the user that completed the job.
- Admin credentials and web host/port are intentionally environment-controlled.
