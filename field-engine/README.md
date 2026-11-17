# PropMon Field Engine

The band-rating engine for the **Ham Ops Console's Field Mode**: PropMon's ratings, for any
location, with no network connection to the NAS. Provided by Ham Shack Automation
(2026-11-17); adapted and run by Ham Ops Console.

At home the Console reads PropMon v2 (`http://192.168.6.29:8076/api/v2/conditions`). In the
field it runs this engine instead, so the time of day follows the sun where the station is.

## Files

| File | What it is |
|---|---|
| `rating_model.json` | The rating model: band thresholds, time-of-day switches, geomagnetic penalties, `model_version`. **Identical to `propmon/rating_model.json`** — never edit it here |
| `field_engine.py` | Reference engine (Python 3.9+, `astral==3.2`). Functions: `load_model`, `time_of_day`, `geo_penalty`, `effective_penalty`, `rate_band`, `rate_all` |
| `test_vectors.json` | Expected results. **EM83 cases come from PropMon's own code**; "elsewhere" cases are this engine's output, for regression |
| `test_field_engine.py` | Runs the vectors: `python test_field_engine.py` |

## How the rating works

- **Score = SFI × geomagnetic penalty**, compared with each band's `[fair, good]` thresholds for
  its current time of day → `good` / `fair` / `poor`.
- **Time of day at the station's location:** 40m–6m dawn = sunrise ±1 h, dusk = sunset ±1 h,
  day between, night outside; 80m switches at sunset/sunrise; 160m at civil dusk/dawn. (Near the
  poles, where the sun doesn't rise or set that day, it falls back to whether the sun is up.)
- **Geomagnetic penalty** from the current K (rounded); held at no better than the recovery cap
  for 12 hours after any 3-hour Kp ≥ 5 (needs recent Kp history — pass `[]` if none).
- **Missing SFI or K → `available: false`, no ratings.** Never show ten "poor" bands for
  missing data.
- **Scale meaning (Dan, 2026-11-07/14):** GOOD = DX on SSB; FAIR = DX on FT8 only, or North
  America only on SSB; POOR = North America only or nothing, even on FT8. A good line to show
  with FAIR: "DX is often workable on FT8 even when SSB is quiet or North America only."
- Ratings are a guide to where to listen first — check the bands anyway.

## Inputs in the field

| Input | Source (what PropMon uses) |
|---|---|
| `sfi` | HamQSL `solarxml.php` → `solarflux` (NOAA as fallback) |
| `k_index` | HamQSL → `kindex` |
| `kp_history` | NOAA `services.swpc.noaa.gov/products/noaa-planetary-k-index.json` (array of `{time_tag, Kp, …}`, UTC) |
| `lat`, `lon`, `tz` | The Pi's position and IANA time zone (e.g. `America/New_York`) |

## Staying in step with PropMon — the update terms

- **Ham Shack Automation keeps testing and may adjust the model** (a recorder comparing ratings
  with PSKReporter reports, plus seasonal on-air sweeps every couple of months). Every change
  updates `propmon/rating_model.json` and bumps `model_version`, and the matching
  `test_vectors.json` is regenerated here.
- **At home, compare** this copy's `model_version` with PropMon v2's top-level
  `"model_version"`. If they differ, take the new `rating_model.json` (and `test_vectors.json`)
  from this folder of `n4mi-shack-server`, run the tests, and only then use it — or show
  "Field engine out of date" until you do.
- **Only the numbers should change.** If the *logic* ever changes (a new rule, a new input),
  Ham Shack Automation will say so in the handoff note; the tests will also fail until the
  engine is updated.

## Not included (possible later Field Mode options)

- **Field Day scale:** for Field Day, North America reach is the goal (many NA stations = good)
  and only non-WARC bands are used (160, 80, 40, 20, 15, 10, 6 m). Not modeled yet; the
  recorder's distance data could test such a scale later.
- Storm/tower alerts (PropMon's Tempest/NWS logic) — home-station specific.

_Model in this folder: `2026-11-17-v3`._
