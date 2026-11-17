# PropMon
**Propagation & Tower Alert Backend** — N4MI | Dan Marshall

A small, always-on Docker service that fetches HF propagation and tower/weather safety data on a
timer, applies band-condition rating logic, and serves the result as flat JSON. No UI, no
persistence beyond an in-memory cache of the last successful fetch. Runs on the NAS, independent
of both shack computers, so consumers stay working even when the ham computer's own
`dashboard_server.py` is stopped. Port **8076**.

---

## Deploy (Portainer, prebuilt image)

The image is built by GitHub Actions and published as `ghcr.io/n4mi73/propmon`
(tags `latest` + short commit SHA). Portainer only pulls it.

1. Stacks → Add stack → **Repository** (NAS environment)
2. Repository URL: `https://github.com/N4MI73/n4mi-shack-server`
3. Reference: `refs/heads/main`
4. **Compose path: `propmon/docker-compose.yml`**
5. Environment variables: `TEMPEST_TOKEN` (see below)
6. GitOps auto-updates: off — update via manual "Pull and redeploy" after the
   **Build images** run on GitHub goes green
7. Deploy, then confirm via the health check endpoint

Rollback: change `:latest` in the compose file to an earlier `:<short-sha>` tag
and redeploy.

Moved here from `streamdeck-hamradio/propmon/` on 2026-10-19, with its Git
history. Until then it was built by Portainer's Repository build method.

---

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `TEMPEST_TOKEN` | Only for tower/weather alerts | WeatherFlow Tempest API token |

Set this in Portainer's stack environment config, never in the repo. The compose file passes
it into the container with `TEMPEST_TOKEN=${TEMPEST_TOKEN}`. PropMon still runs and serves
propagation data without it; tower/weather alerts just stay at `NONE`.

---

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/instrument/propagation` | Main data endpoint |
| `GET /healthz` | Liveness check — `{"status": "ok"}` whenever the process is up, regardless of fetch/data state |
| `GET /api/v2/conditions` | Richer conditions for the dashboard and future displays (added 2026-10-20). Same ratings as v1 plus each band's time of day, geomagnetic penalty and 12-hour recovery state, recent Kp, sun times, Bz, proton/electron flux, aurora, signal noise, geomagnetic field text, NOAA R/S/G scales with the 3-day forecast, and tower detail (every NWS alert, Tempest readings). Additive — v1 is unchanged. Since 2026-11-17 also a top-level `model_version` (the rating model in use). Since 2026-11-07 also a top-level `period` (`dawn`/`day`/`dusk`/`night` — the 40m–6m time of day at EM83; 160m and 80m switch only day/night, see `bands[].tod`). On the NOAA fallback (`source: "noaa"`, summary `"Data unavailable"`) the ratings are not usable and `bands[].tod` is null |

**Consumers** (check each before changing an endpoint): the ESP32 Propagation Monitor and DX Monitor (v1); the Ham Ops Console (v1, moving to v2); the Propagation Dashboard on n4misrv (v2, since 2026-11-07); the Propagation Recorder (v2, temporary). The v1 contract: same 10-band order and names, statuses only `good`/`fair`/`poor`, a non-empty `alerts` array, and the fields `updated`, `summary`, `sfi`, `a_index`, `k_index`, `sunspots`, `xray`, `solar_wind` unchanged.

### Response shape

```json
{
  "updated": "2026-07-16T01:15:00Z",
  "summary": "GOOD: 160m 40m 30m | FAIR: 80m 20m 17m",
  "sfi": 100,
  "a_index": 14,
  "k_index": 1,
  "sunspots": 25,
  "xray": "C4.1",
  "solar_wind": 654.6,
  "bands": [
    {"band": "160m", "status": "good"},
    {"band": "80m",  "status": "fair"}
  ],
  "alerts": [
    {"category": "tower", "level": "NONE", "message": "No active alerts"}
  ],
  "tower_status": "NONE"
}
```

`alerts` is never an empty array in steady state — a calm system returns a single `NONE`-level
placeholder entry, not nothing, so a consumer never needs a special empty-array case.

---

## Data sources & refresh

| Data | Source | Refresh |
|---|---|---|
| SFI, sunspots, K/A-index, X-ray, solar wind | HamQSL XML (primary), NOAA SWPC K-index (fallback) | 5 min |
| Lightning, wind gust | WeatherFlow Tempest | 2 min |
| Severe weather alerts | NWS Alerts (zone-based) | 2 min |

On fetch failure, PropMon keeps serving the last successful result with its original timestamp
rather than erroring — a consumer can tell "service is down" (connection refused) apart from
"data is stale" (old `updated` timestamp).

---

## Band rating

10 bands (160m–6m), rated `good`/`fair`/`poor`. **Score = SFI × geomagnetic penalty**, compared
with each band's `[fair, good]` thresholds for its current time of day at EM83.

**The rating model lives in [`rating_model.json`](rating_model.json)** (since 2026-11-17): band
thresholds, time-of-day switches and the geomagnetic penalties. PropMon loads it at startup and
refuses to start if it is missing or invalid. v2 publishes its `model_version`, so a copy of the
model elsewhere (the Ham Ops Console's Field Mode engine) can tell when it is out of date.
**To change the model:** edit the file, bump `model_version`, rebuild and redeploy.

- **Time of day (real sun at EM83, `astral`):** 40m–6m: dawn = sunrise ±1 h, dusk = sunset ±1 h,
  day between, night outside. 80m switches at sunset/sunrise; 160m at civil dusk/dawn.
- **Geomagnetic penalty (model 2026-11-17-v3):** K0–2 ×1.0, K3 ×0.90, K4 ×0.80, K5 ×0.70,
  K6 ×0.50, K7+ ×0.30 (fractional Kp rounded). For 12 hours after any 3-hour Kp ≥5 the penalty
  is no better than ×0.80.
- **Tuned against on-air data:** the 2026-11-17 values come from ten days of N4MI's SSB/FT8 band
  sweeps, PSKReporter reports heard in EM83 and logged contacts (Propagation Engine Roadmap,
  Ham Shack Automation). Rating scale: GOOD = DX on SSB; FAIR = DX on FT8 only, or North
  America only on SSB; POOR = North America only or nothing, even on FT8.
- Ratings are a guide to where to listen first, not a verdict — check the bands anyway.

Deliberately out of scope: atmospheric noise/QRN modeling (summer static on 160m/80m/40m).
PropMon's job is ionospheric band condition only.

---

## Related Projects

**Consumers of `/api/instrument/propagation`:** the ESP32 instrument below, and the DX Monitor
server (`dxmon`, NAS port 8083), which reads the band ratings for its condition dots. Keep the
response shape stable: the firmware expects the same 10-band order every time and a non-empty
`alerts` array.

**[N4MI Propagation Monitor](https://github.com/N4MI73/n4mi-propagation-monitor)** — an
ESP32-based desktop hardware instrument (LilyGO T-Encoder Pro, round AMOLED display) that polls
this service's `/api/instrument/propagation` endpoint to drive a live propagation display. This is
PropMon's primary consumer today, but PropMon itself is general-purpose — anything that can make
an HTTP GET request can use this API. See that repo's own README for firmware setup and current
build status.

---

## File structure

```
propmon/
  propmon_server.py    Flask app, background fetch threads, band-rating logic
  rating_model.json    Rating model: thresholds, time-of-day switches, penalties (model_version)
  Dockerfile            Python 3.12-slim, builds from requirements.txt (built by GitHub Actions)
  docker-compose.yml    Portainer stack definition (pulls ghcr.io/n4mi73/propmon)
  requirements.txt      flask, requests, astral
```
---

## Blog Post

This project is also described on my blog, n4mi.tech, in this article [PropMon – a Desktop Propagation Monitor](https://n4mi.tech/propmon-a-desktop-propagation-monitor/)

---
