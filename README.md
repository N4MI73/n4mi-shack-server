# N4MI Shack Server

Always-on, server-hosted versions of N4MI's ham shack web apps, run as Docker
containers on a small Linux server and deployed with Portainer straight from
this repo.

This is the **server edition**. The original Windows/local-computer versions,
and the "Stream Deck for Ham Radio" blog series they come from, live in
[streamdeck-hamradio](https://github.com/N4MI73/streamdeck-hamradio) and still
work as they are.

| App | Folder | Port | What it does |
|---|---|---|---|
| HamShackFeed Pro | `hamshackfeedpro/` | 8074 | Ham radio blogs, podcasts and YouTube channels in one feed, with read state shared across devices (Flask + SQLite) |
| Propagation Dashboard | `dashboard/` | 8073 | HF + 6m band conditions, solar data, storm/lightning and radio-blackout alerts, active DXpeditions (Python standard library only) |
| PropMon | `propmon/` | 8076 | Propagation and tower-alert JSON service for the ESP32 desk instrument and DX Monitor; runs on the NAS (Flask). See `propmon/README.md` |

## Deploying (Portainer)

Images are built on GitHub, not on the server. Every push to `main` that
touches an app folder runs the **Build images** workflow
(`.github/workflows/build-images.yml`), which publishes:

- `ghcr.io/n4mi73/hamshackfeedpro`
- `ghcr.io/n4mi73/propdashboard`
- `ghcr.io/n4mi73/propmon`

Each image is tagged `latest` plus the short commit SHA, for rollback.
Portainer then only pulls finished images. This is deliberate: Portainer can't
build images on an Agent-connected environment, where BuildKit fails with
`failed to list workers ... http2: frame too large`
([portainer#13255](https://github.com/portainer/portainer/issues/13255)).

Each app is its own stack. In Portainer: **Stacks → Add stack → Repository**

| Setting | HamShackFeed Pro | Propagation Dashboard | PropMon |
|---|---|---|---|
| Portainer environment | n4misrv | n4misrv | NAS |
| Repository URL | `https://github.com/N4MI73/n4mi-shack-server` | same | same |
| Reference | `refs/heads/main` | same | same |
| Compose path | `hamshackfeedpro/docker-compose.yml` | `dashboard/docker-compose.yml` | `propmon/docker-compose.yml` |
| Environment variables | none | `TEMPEST_TOKEN` (see below) | `TEMPEST_TOKEN` (its own token) |

**To update an app:**

1. Change the code on GitHub.
2. Wait for the **Build images** run to go green (Actions tab, about a minute).
3. Use **Pull and redeploy** on the stack. The compose files set
   `pull_policy: always`, so every redeploy fetches the newest image.

**To roll back:** change `:latest` in the compose file to an earlier
`:<short-sha>` tag and redeploy.

### Data

- **HamShackFeed Pro** stores its SQLite database in a bind mount,
  `/srv/docker/hamshackfeedpro/data` on the host. Keep it a bind mount, not a
  named Docker volume, so host-level backups pick it up. Back up SQLite with
  `sqlite3 .backup` (or Python's `sqlite3` backup API), never by copying the
  live file: recent changes can sit in the `-wal` side file.
- **Propagation Dashboard** and **PropMon** have no persistent data.

### Secrets

The dashboard and PropMon each read a WeatherFlow Tempest token from the
`TEMPEST_TOKEN` environment variable (use a separate token for each) (Tempest: Settings → Data Authorizations). Set it in the
Portainer stack's environment variables. **Never commit a token to this repo.**
Without a token, the storm monitor still runs on NWS alerts alone.

Station-specific values (Tempest station ID, NWS zone, the DXMon address) are
constants near the top of `dashboard/dashboard_server.py`. Change them for your
own station.

## Running without Docker

Both apps still run directly with Python 3.13 (`python server.py` or
`python dashboard_server.py`). Optional environment variables for HamShackFeed
Pro: `HSF_DB_PATH` (database location) and `HSF_OPEN_BROWSER=0` (don't open a
browser at startup).

73 de N4MI — [n4mi.tech](https://n4mi.tech)
