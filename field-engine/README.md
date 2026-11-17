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
| `field_engine.py` | Reference engine (Python 3.9+, `astral==3.2`). Functions: `load_model`, `time_of_day`, `geo_penalty`, `effective_penalty`, `rate_band`, `rate_all` (`scale="dx"` default, or `"field_day"`) |
| `test_vectors.json` | Expected results. **EM83 dx cases come from PropMon's own code**; "elsewhere" cases are this engine's output, for regression; `field_day` cases use the same inputs with the Field Day thresholds applied directly |
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
- **The Field Day scale has its own `field_day.version`,** which PropMon doesn't report (PropMon
  has no Field Day scale). Changes to it come with a note from Ham Shack Automation. To check
  automatically, compare both versions with the public copy at
  `https://raw.githubusercontent.com/N4MI73/n4mi-shack-server/main/field-engine/rating_model.json`.
- **Only the numbers should change.** If the *logic* ever changes (a new rule, a new input),
  Ham Shack Automation will say so in the handoff note; the tests will also fail until the
  engine is updated.

## Field Day scale — `rate_all(..., scale="field_day")` (added 2026-11-17)

For Field Day the goal is **North America reach**, not DX, and only the non-WARC bands are used.
Same inputs, same score (SFI × geomagnetic penalty), same time-of-day rules; its own thresholds
in the `field_day` block of `rating_model.json`, with its **own `version`** (`2026-11-17-fd1`), so
it never changes `model_version`. Bands, in order: 160, 80, 40, 20, 15, 10, 6 m. Results carry
`"scale": "field_day"` and `"scale_version"`.

| Word | Meaning (for the console's Help text) |
|---|---|
| good | Wide North America coverage: phone contacts across much of the country likely. |
| fair | Some North America reach: workable but thinner or regional; easier on CW/digital. |
| poor | Little or no North America reach. |

- **NA reach = stations within 4,000 km** of the station (covers the US, Canada and the
  Caribbean from the Southeast).
- **6m is always `poor`, with a `note`:** its Field Day openings are mostly sporadic E, which
  SFI and K can't forecast — check the band.
- **160m daytime is always `poor`** (daytime 160 m is local ground-wave only).
- **What backs it:** Propagation Recorder data at EM83, 2026-11-07 → 11-17 — 3,237 band-slots with
  a receiver in EM83 reporting to PSKReporter (20 m 912, 40 m 877, 80 m 491, 15 m 387, 160 m 328,
  10 m 242). Each slot was graded by NA stations heard against that band's own busiest hours
  (GOOD ≥ 40% of its 90th-percentile count and ≥ 15 stations; FAIR ≥ 10% and ≥ 5). The
  thresholds were fitted to those grades, starting from the DX thresholds. Exact agreement 62%
  (the DX scale used as-is: 45%); "open for NA or not" right 82% of the time (the console's
  stopgap, DX FAIR or better: 76%). Weakest: 160 m (47%, thin data, mostly one receiver) and 80 m
  (51%).
- **Season:** November, SFI 120–145, mostly quiet (K0–3) — **suited to Winter Field Day (late
  January)**. **Not validated for ARRL Field Day (June):** short nights, more daytime absorption,
  summer static on 160/80/40 m and sporadic E on 10/6 m. Plan (Dan): run the recorder in May–June
  and add June values before ARRL Field Day. Low and very high SFI, and storms, are also
  extrapolated.
- Activity on Field Day weekend is far higher than in the data; the grades were relative to each
  band's own busy hours for that reason.

## Not included

- Storm/tower alerts (PropMon's Tempest/NWS logic) — home-station specific.

_Model in this folder: `2026-11-17-v3`; Field Day scale `2026-11-17-fd1`._
