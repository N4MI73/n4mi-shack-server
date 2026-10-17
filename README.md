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

## Deploying (Portainer)

Each app is its own stack. In Portainer: **Stacks → Add stack → Repository**

| Setting | HamShackFeed Pro | Propagation Dashboard |
|---|---|---|
| Repository URL | `https://github.com/N4MI73/n4mi-shack-server` | same |
| Reference | `refs/heads/main` | same |
| Compose path | `hamshackfeedpro/docker-compose.yml` | `dashboard/docker-compose.yml` |
| Environment variables | none | `TEMPEST_TOKEN` (see below) |

After changing code on GitHub, use **Pull and redeploy** on the stack. The
compose files set `pull_policy: build`, so the image is rebuilt from the new
source.

### Data

- **HamShackFeed Pro** stores its SQLite database in a bind mount,
  `/srv/docker/hamshackfeedpro/data` on the host. Keep it a bind mount, not a
  named Docker volume, so host-level backups pick it up. Back up SQLite with
  `sqlite3 .backup` (or Python's `sqlite3` backup API), never by copying the
  live file: recent changes can sit in the `-wal` side file.
- **Propagation Dashboard** has no persistent data.

### Secrets

The dashboard reads a WeatherFlow Tempest token from the `TEMPEST_TOKEN`
environment variable (Tempest: Settings → Data Authorizations). Set it in the
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
