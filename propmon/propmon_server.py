"""
PropMon Service — N4MI Desktop Instrument Series
==================================================

Small, always-on JSON data service for ESP32-based desktop instruments
(LilyGO T-Encoder Pro, starting with the Propagation Monitor).

Runs independently of dashboard_server.py (ham computer, port 8073) and of
any Windows computer being powered on at all — designed to live on the NAS
as a Docker container, so desktop instruments keep working even when the
ham shack dashboard server is intentionally stopped.

Data sources:
  - HamQSL XML feed (primary)      -> solar/band data
  - NOAA SWPC (fallback)           -> solar/band data, if HamQSL is down
  - WeatherFlow Tempest API        -> lightning/wind (tower alerts)
  - NWS Alerts API                 -> severe weather (tower alerts)

Behavior on fetch failure: keep serving the last known-good result with its
original "updated" timestamp, rather than erroring out. This lets firmware
tell the difference between "service is down" (connection refused) and
"data is stale" (old timestamp) -- two different, honestly-represented
failure modes.

No persistence. No database. Pure in-memory cache. Restarting this
container loses nothing except a few minutes of freshness until the next
fetch cycle completes.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from xml.etree import ElementTree

import requests
from astral import LocationInfo
from astral.sun import sun
from flask import Flask, jsonify

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HAMQSL_URL = "https://www.hamqsl.com/solarxml.php"
NOAA_KINDEX_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
NOAA_SCALES_URL = "https://services.swpc.noaa.gov/products/noaa-scales.json"

# WeatherFlow Tempest -- station ID per project context (N4MI base context)
TEMPEST_STATION_ID = "159572"
TEMPEST_TOKEN = os.environ.get("TEMPEST_TOKEN", "")  # set via docker-compose.yml environment
TEMPEST_URL = "https://swd.weatherflow.com/swd/rest/better_forecast"

# NWS Alerts -- by zone (Columbia County GA), matching dashboard_server.py exactly,
# rather than a lat/lon point query.
NWS_ZONE = "GAC073"
NWS_ALERTS_URL = f"https://api.weather.gov/alerts/active?zone={NWS_ZONE}"
NWS_USER_AGENT = "N4MI-PropMon/1.0 N4MI operator"

# Lightning thresholds -- ported exactly from dashboard_server.py
LIGHTNING_WARNING_KM = 40    # WARNING if lightning within 40km
LIGHTNING_CAUTION_KM = 80    # CAUTION if lightning within 80km
LIGHTNING_RECENT_MINS = 30   # only counts if strike was within the last 30 min

# Wind gust threshold -- ported exactly from dashboard_server.py (m/s, ~35mph)
WIND_GUST_CAUTION_MPS = 15

SOLAR_FETCH_INTERVAL_SEC = 300   # 5 minutes
WEATHER_FETCH_INTERVAL_SEC = 120  # 2 minutes -- storm conditions change faster than solar data

# Data considered "stale" past this many minutes (firmware also checks this,
# this is just used server-side for logging/sanity, not enforced as an error)
STALE_THRESHOLD_MIN = 30

HTTP_TIMEOUT_SEC = 10

PORT = 8076

# EM83 station location -- used only for real sunrise/sunset/civil-twilight
# calculation (see the band-rating sections below). Approximate coordinates for
# Grovetown, GA. Not tied to the Tempest station or NWS zone configs above;
# those are separate identifiers for separate APIs describing the same
# physical location.
EM83_TZ = ZoneInfo("America/New_York")
EM83_LOCATION = LocationInfo("Grovetown", "USA", "America/New_York", 33.45, -82.19)

# ---------------------------------------------------------------------------
# Band rating thresholds
# ---------------------------------------------------------------------------
# Ported exactly from N4MI_PropagationDashboard.html's rateBand()/
# getTimeOfDay() JS (verbatim model, not re-derived). The dashboard's model
# is 8 bands (80m-6m). The PDF brief's worked example listed 10 bands
# including 160m and 30m, so per the brief we added 160m (below 80m's
# thresholds) and 30m (between 40m and 20m's thresholds), following the
# same pattern as neighboring bands. These two additions are new threshold
# tuning, not verbatim ports -- the *threshold numbers* were interpolated
# from neighboring bands, not the time-of-day logic itself.
#
# Model: geomagnetic penalty multiplies SFI itself (not a hard per-band
# K-index cutoff): kIdx<=2 -> 1.0x, kIdx<=4 -> 0.65x, else -> 0.25x.
# score = sfi * geo_penalty; band is "good" if score >= goodThresh,
# "fair" if score >= fairThresh, else "poor".
#
# --- 160m/80m time-of-day fix (2026-07-15/16) --- [HISTORY: the fixed-clock
# get_time_of_day()/get_low_band_tod() described here were replaced by
# get_band_tod() on 2026-10-19 -- see "Stage 2 engine changes" below.]
# get_time_of_day() below uses fixed UTC-hour buckets, shared verbatim with
# the dashboard's JS. For most bands this is fine -- their day/dusk/night
# thresholds differ gradually, so a seasonal mismatch between the fixed
# clock and real sunset only nudges the rating slightly. 160m and 80m are
# different: both have a fully-closed "day" ([999, 999], impossible to
# reach) followed by a wide-open "night"/"dusk" threshold, a near-binary
# jump. Combined with the fixed clock's several-hour seasonal drift against
# real sunset (see propmon_160m_timeofday_issue.md), this produced a real
# bug: 160m rated GOOD during full daylight on 2026-07-15, 90+ minutes
# before actual sunset.
#
# Fix: 160m and 80m no longer use get_time_of_day()/the dusk+dawn buckets
# at all. They use get_low_band_tod() instead, which computes real civil
# dusk/dawn for EM83 (via the `astral` library) and returns a simple
# "day"/"night" state -- closed until it's genuinely dark, open once it
# is, confirmed against Dan's own on-air experience (160m/80m need to be
# genuinely dark, not just dusk, before they reliably open). Per Dan's
# direction, both bands share this identical logic rather than having
# separate offsets/ramps for each -- the difference between them wasn't
# judged large enough to justify separate models. Their "dawn"/"dusk"
# threshold entries below are removed since they're no longer looked up;
# only "day" and "night" are used for these two bands now.
#
# 30m was not reported as broken and is NOT touched by this fix -- it
# still uses the standard fixed-clock get_time_of_day(), same as the other
# 7 unaffected bands. Worth a look later per the original handoff note if
# it ever shows a similar symptom.
#
# Deliberately NOT included: any atmospheric-noise/QRN modeling for
# summer static on 160m/80m/40m. Dan already knows to expect that
# seasonally; PropMon's job is ionospheric band condition only.

# --- Stage 2 engine changes (2026-10-19, Dan's decisions) ---
# 1. 40m-6m: time of day follows the REAL sun at EM83 -- dawn = sunrise
#    +/-1 h, dusk = sunset +/-1 h, day between, night outside (replaces the
#    fixed 12-22 UTC clock, which drifted 2+ hours against summer sunset).
# 2. 12-hour storm recovery: if any 3-hour Kp in the last 12 h was storm
#    level (>=5), the geomagnetic penalty stays at no better than x0.65.
# 3. 80m opens at sunset and closes at sunrise ("80m can start working
#    around dusk"); 160m keeps the civil-twilight switch ("needs total
#    darkness") and gets stricter night thresholds than 80m.
# 4. (alerts -- see CAUTION_EVENTS_EXACT below)
# 5. Finer K tiers aligned with NOAA's G-scale (see get_geo_penalty).
# Also: 30m now uses the dashboard's on-air-validated thresholds
# (2026-07-19) instead of the v1 interpolated values.
# All values are tunable starting points, not measurements.

BAND_ORDER = ["160m", "80m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"]

# 160m: civil dusk/dawn switch. 80m: sunset/sunrise switch. Both use only
# "day" and "night" thresholds.
CIVIL_TWILIGHT_BANDS = {"160m"}
SUNSET_BANDS = {"80m"}

DAWN_DUSK_HALF_WIDTH = timedelta(hours=1)   # decision 1
RECOVERY_HOURS = 12                          # decision 2
RECOVERY_STORM_KP = 5                        # G1 and above
RECOVERY_PENALTY_CAP = 0.65

# { band: { tod: [fairThresh, goodThresh] } }
BAND_MODELS = {
    "160m": {"day": [999, 999], "night": [80, 100]},   # was [60, 80] -- now stricter than 80m
    "80m":  {"day": [999, 999], "night": [70, 90]},
    "40m":  {"night": [70, 90],  "dawn": [75, 95],   "day": [130, 160], "dusk": [75, 95]},
    # 30m: dashboard's validated values (was night 82/107, dawn/dusk 80/102, day 105/132)
    "30m":  {"night": [75, 95],  "dawn": [75, 95],   "day": [95, 120],  "dusk": [75, 95]},
    "20m":  {"night": [95, 125], "dawn": [85, 110],  "day": [80, 105],  "dusk": [85, 110]},
    "17m":  {"night": [105, 135], "dawn": [95, 120],  "day": [90, 115],  "dusk": [95, 120]},
    "15m":  {"night": [120, 150], "dawn": [110, 140], "day": [105, 135], "dusk": [110, 140]},
    "12m":  {"night": [140, 170], "dawn": [130, 160], "day": [120, 150], "dusk": [130, 160]},
    "10m":  {"night": [155, 185], "dawn": [145, 175], "day": [130, 165], "dusk": [145, 175]},
    "6m":   {"night": [999, 999], "dawn": [160, 190], "day": [150, 185], "dusk": [160, 190]},
}


def get_geo_penalty(k_index: float) -> float:
    """Geomagnetic penalty multiplier on SFI (decision 5, 2026-10-19).
    K0-2 x1.0, K3 x0.85, K4 x0.65, K5 (G1) x0.45, K6 (G2) x0.30,
    K7+ (G3-G5) x0.15. Fractional Kp (e.g. 2.67) is rounded first."""
    k = round(k_index)
    if k <= 2:
        return 1.0
    if k == 3:
        return 0.85
    if k == 4:
        return 0.65
    if k == 5:
        return 0.45
    if k == 6:
        return 0.30
    return 0.15


def storm_in_recovery_window(kp_history: list, now_utc: datetime) -> bool:
    """True if any 3-hour Kp reading in the last RECOVERY_HOURS was storm
    level. kp_history: list of (datetime_utc, kp) from NOAA."""
    cutoff = now_utc - timedelta(hours=RECOVERY_HOURS)
    return any(t >= cutoff and round(kp) >= RECOVERY_STORM_KP for t, kp in kp_history)


def effective_penalty(k_index: float, kp_history: list, now_utc: datetime) -> float:
    """Current-K penalty, capped at RECOVERY_PENALTY_CAP during the 12-hour
    recovery window after a storm (decision 2). The worse of the two wins."""
    p = get_geo_penalty(k_index)
    if storm_in_recovery_window(kp_history, now_utc):
        p = min(p, RECOVERY_PENALTY_CAP)
    return p


def _sun_times(now_utc: datetime) -> dict:
    local_now = now_utc.astimezone(EM83_TZ)
    return sun(EM83_LOCATION.observer, date=local_now.date(), tzinfo=EM83_TZ)


def get_band_tod(band: str, now_utc: datetime) -> str:
    """Time-of-day bucket for one band at EM83 (decisions 1 and 3)."""
    s = _sun_times(now_utc)
    now = now_utc.astimezone(EM83_TZ)
    if band in CIVIL_TWILIGHT_BANDS:
        # 160m: night from the end of civil dusk to the start of civil dawn.
        return "night" if (now < s["dawn"] or now >= s["dusk"]) else "day"
    if band in SUNSET_BANDS:
        # 80m: night from sunset to sunrise.
        return "night" if (now < s["sunrise"] or now >= s["sunset"]) else "day"
    w = DAWN_DUSK_HALF_WIDTH
    if s["sunrise"] - w <= now < s["sunrise"] + w:
        return "dawn"
    if s["sunset"] - w <= now < s["sunset"] + w:
        return "dusk"
    if s["sunrise"] + w <= now < s["sunset"] - w:
        return "day"
    return "night"


def rate_band(sfi: float, penalty: float, band: str, tod: str) -> str:
    """'good', 'fair' or 'poor': score = SFI x geomagnetic penalty, compared
    against the band's [fair, good] thresholds for this time of day."""
    fair_thresh, good_thresh = BAND_MODELS[band][tod]
    score = sfi * penalty
    if score >= good_thresh:
        return "good"
    elif score >= fair_thresh:
        return "fair"
    else:
        return "poor"


# ---------------------------------------------------------------------------
# Shared in-memory cache
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache = {
    "solar": {
        "updated": None,
        "sfi": None,
        "a_index": None,
        "k_index": None,
        "sunspots": None,
        "xray": None,
        "solar_wind": None,
    },
    "tower_status": "NONE",
    "alerts": [],
}

_kp_history = []   # [(datetime_utc, kp)], refreshed by solar_fetch_loop
_kp_history_at = None
_noaa_scales = None   # refreshed by solar_fetch_loop (v2 only)
_noaa_scales_at = None
_last_engine = {}     # per-band time of day and penalty from the last rating
_weather = {"tempest": None, "nws_alerts": [], "updated": None,
            "tempest_error": None, "nws_error": None}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("propmon")


# ---------------------------------------------------------------------------
# Solar / band data fetch (HamQSL primary, NOAA fallback)
# ---------------------------------------------------------------------------

def fetch_hamqsl():
    """Fetch and parse HamQSL's solarxml.php feed. Returns a dict of solar
    values, or None on failure."""
    resp = requests.get(HAMQSL_URL, timeout=HTTP_TIMEOUT_SEC)
    resp.raise_for_status()
    root = ElementTree.fromstring(resp.content)

    solardata = root.find("solardata")
    if solardata is None:
        raise ValueError("HamQSL response missing <solardata>")

    def _text(tag, cast=str, default=None):
        el = solardata.find(tag)
        if el is None or el.text is None:
            return default
        try:
            return cast(el.text.strip())
        except (ValueError, TypeError):
            return default

    return {
        "sfi": _text("solarflux", int),
        "a_index": _text("aindex", int),
        "k_index": _text("kindex", int),
        "sunspots": _text("sunspots", int),
        "xray": _text("xray", str),
        "solar_wind": _text("solarwind", float),
        # v2-only fields (2026-10-20). Element names verified live 2026-10-20;
        # HamQSL itself spells the electron-flux element "electonflux".
        "bz": _text("magneticfield", float),
        "proton_flux": _text("protonflux", float),
        "electron_flux": _text("electonflux", float),
        "aurora": _text("aurora", int),
        "signal_noise": _text("signalnoise", str),
        "geomag_field": _text("geomagfield", str),
        "hamqsl_updated": _text("updated", str),
    }


def fetch_kp_history():
    """NOAA SWPC planetary K-index, 3-hour values for the last ~7 days, as a
    list of (datetime_utc, kp) oldest first.

    Response shape (verified live 2026-10-19): a JSON array of objects, e.g.
    {"time_tag": "2026-10-18T18:00:00", "Kp": 1.67, "a_running": 6,
    "station_count": 8}; time_tag is UTC without a suffix. The older
    array-of-arrays shape (header row first, Kp in column 1) is still
    accepted, in case NOAA reverts."""
    resp = requests.get(NOAA_KINDEX_URL, timeout=HTTP_TIMEOUT_SEC)
    resp.raise_for_status()
    data = resp.json()
    out = []
    for row in data:
        if isinstance(row, dict):
            tag, kp = row.get("time_tag"), row.get("Kp")
        elif isinstance(row, list) and len(row) >= 2:
            tag, kp = row[0], row[1]
        else:
            continue
        try:
            t = datetime.fromisoformat(str(tag).replace("Z", "")[:19]).replace(tzinfo=timezone.utc)
            out.append((t, float(kp)))
        except (ValueError, TypeError):
            continue  # header row or malformed entry
    if not out:
        raise ValueError("NOAA k-index response had no usable rows")
    out.sort(key=lambda r: r[0])
    return out


def fetch_noaa_fallback(kp_history=None):
    """Fallback solar data source -- NOAA SWPC planetary K-index.
    Much sparser than HamQSL; only fills in k_index. Used only if HamQSL
    is unreachable. (Fixed 2026-10-19: NOAA had changed the response to an
    array of objects, which broke the old header-row parser.)"""
    history = kp_history or fetch_kp_history()
    k_index = round(history[-1][1])
    return {
        "sfi": None,
        "a_index": None,
        "k_index": k_index,
        "sunspots": None,
        "xray": None,
        "solar_wind": None,
    }


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def fetch_noaa_scales():
    """NOAA SWPC space-weather scales: R (radio blackouts), S (solar
    radiation storms), G (geomagnetic storms).

    Response shape (verified live 2026-10-20): an object keyed "-1" (the
    past 24 hours' maximum), "0" (current) and "1"-"3" (3-day forecast).
    Each entry: {"DateStamp", "TimeStamp", "R": {"Scale", "Text", "MinorProb",
    "MajorProb"}, "S": {"Scale", "Text", "Prob"}, "G": {"Scale", "Text"}}.
    Observed entries carry Scale/Text (probabilities null); forecast entries
    carry R/S probabilities (Scale/Text null) and a predicted G scale."""
    resp = requests.get(NOAA_SCALES_URL, timeout=HTTP_TIMEOUT_SEC)
    resp.raise_for_status()
    data = resp.json()

    def observed(e):
        return {
            "date": e.get("DateStamp"),
            "time": e.get("TimeStamp"),
            "R": {"scale": _int_or_none(e["R"].get("Scale")), "text": e["R"].get("Text")},
            "S": {"scale": _int_or_none(e["S"].get("Scale")), "text": e["S"].get("Text")},
            "G": {"scale": _int_or_none(e["G"].get("Scale")), "text": e["G"].get("Text")},
        }

    def forecast(e):
        return {
            "date": e.get("DateStamp"),
            "R": {"minor_prob": _int_or_none(e["R"].get("MinorProb")),
                  "major_prob": _int_or_none(e["R"].get("MajorProb"))},
            "S": {"prob": _int_or_none(e["S"].get("Prob"))},
            "G": {"scale": _int_or_none(e["G"].get("Scale")), "text": e["G"].get("Text")},
        }

    if "0" not in data:
        raise ValueError("NOAA scales response missing current entry")
    return {
        "past_24h": observed(data["-1"]) if "-1" in data else None,
        "current": observed(data["0"]),
        "forecast": [forecast(data[k]) for k in ("1", "2", "3") if k in data],
    }


def build_bands_and_summary(solar: dict, kp_history=None, now_utc=None):
    """Computes per-band ratings and a short human-readable summary string,
    given current SFI/K-index. The summary mirrors the dashboard HTML's own
    statusRow grouping (e.g. "GOOD: 20m 17m") rather than picking one
    arbitrary 'best' band -- the dashboard itself never reduces this to a
    single band, so neither does PropMon."""
    sfi = solar.get("sfi")
    k_index = solar.get("k_index")

    if sfi is None or k_index is None:
        # Not enough data to rate bands meaningfully.
        bands = [{"band": b, "status": "poor"} for b in BAND_ORDER]
        return bands, "Data unavailable"

    now_utc = now_utc or datetime.now(timezone.utc)
    penalty = effective_penalty(k_index, kp_history or [], now_utc)

    bands = []
    tods = {}
    for b in BAND_ORDER:
        tod = get_band_tod(b, now_utc)
        tods[b] = tod
        bands.append({"band": b, "status": rate_band(sfi, penalty, b, tod)})
    # Extra detail for the v2 endpoint only -- the v1 bands list stays
    # exactly {"band", "status"}.
    _last_engine.update({"tods": tods, "penalty": penalty, "computed_at": now_utc})

    groups = {"good": [], "fair": [], "poor": []}
    for b in bands:
        groups[b["status"]].append(b["band"])

    parts = []
    for level in ("good", "fair"):  # poor bands aren't worth calling out in the summary
        if groups[level]:
            parts.append(f"{level.upper()}: {' '.join(groups[level])}")
    summary = " | ".join(parts) if parts else "POOR: all bands"

    return bands, summary


def solar_fetch_loop():
    """Background thread: refreshes solar/band data every
    SOLAR_FETCH_INTERVAL_SEC seconds, forever."""
    while True:
        # Kp history drives the 12-hour storm-recovery rule (and the NOAA
        # fallback). Fetched every cycle; on failure the last good copy is
        # kept, and with none at all the rule simply doesn't apply.
        global _kp_history, _kp_history_at, _noaa_scales, _noaa_scales_at
        try:
            _kp_history = fetch_kp_history()
            _kp_history_at = datetime.now(timezone.utc)
        except Exception as e:
            log.warning("NOAA Kp history fetch failed (%s) -- keeping last copy", e)
        try:
            _noaa_scales = fetch_noaa_scales()
            _noaa_scales_at = datetime.now(timezone.utc)
        except Exception as e:
            log.warning("NOAA scales fetch failed (%s) -- keeping last copy", e)

        try:
            solar = fetch_hamqsl()
            log.info("HamQSL fetch OK: SFI=%s K=%s", solar.get("sfi"), solar.get("k_index"))
        except Exception as e:
            log.warning("HamQSL fetch failed (%s), trying NOAA fallback", e)
            try:
                solar = fetch_noaa_fallback(_kp_history)
                log.info("NOAA fallback fetch OK: K=%s", solar.get("k_index"))
            except Exception as e2:
                log.error("NOAA fallback also failed (%s) -- keeping last known-good data", e2)
                solar = None

        if solar is not None:
            bands, summary = build_bands_and_summary(solar, _kp_history)
            with _cache_lock:
                _cache["solar"] = {
                    "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "sfi": solar.get("sfi"),
                    "a_index": solar.get("a_index"),
                    "k_index": solar.get("k_index"),
                    "sunspots": solar.get("sunspots"),
                    "xray": solar.get("xray"),
                    "solar_wind": solar.get("solar_wind"),
                    "extra": {k: solar.get(k) for k in (
                        "bz", "proton_flux", "electron_flux", "aurora",
                        "signal_noise", "geomag_field", "hamqsl_updated")},
                    "source": "hamqsl" if solar.get("hamqsl_updated") is not None else "noaa",
                    "bands": bands,
                    "summary": summary,
                }
        # else: leave _cache["solar"] untouched -- last known-good data
        # (with its original timestamp) keeps being served.

        time.sleep(SOLAR_FETCH_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# Tower / weather alert fetch (Tempest + NWS)
# ---------------------------------------------------------------------------

def _redact(text) -> str:
    """Remove the Tempest token from error text: a failed request's error
    message can include the full URL, and the token is a URL parameter."""
    text = str(text)
    return text.replace(TEMPEST_TOKEN, "***") if TEMPEST_TOKEN else text


def fetch_tempest():
    """Fetch current conditions from WeatherFlow Tempest station, via the
    same better_forecast endpoint dashboard_server.py uses. Returns a dict
    with lightning distance/age/1hr-count and wind gust (in mph), or raises
    on failure."""
    if not TEMPEST_TOKEN:
        raise RuntimeError("TEMPEST_TOKEN not configured")

    url = f"{TEMPEST_URL}?station_id={TEMPEST_STATION_ID}&token={TEMPEST_TOKEN}"
    resp = requests.get(url, headers={"User-Agent": "N4MI-PropMon/1.0"}, timeout=HTTP_TIMEOUT_SEC)
    resp.raise_for_status()
    data = resp.json()

    cc = data.get("current_conditions", {})
    lightning_km = cc.get("lightning_strike_last_distance")
    lightning_epoch = cc.get("lightning_strike_last_epoch")
    lightning_1hr = cc.get("lightning_strike_count_last_1hr", 0) or 0
    wind_gust_mps = cc.get("wind_gust")  # Tempest reports m/s

    lightning_ago_min = None
    if lightning_epoch:
        now_utc = datetime.now(timezone.utc).timestamp()
        lightning_ago_min = round((now_utc - lightning_epoch) / 60, 1)

    return {
        "lightning_km": lightning_km,
        "lightning_ago_min": lightning_ago_min,
        "lightning_1hr": lightning_1hr,
        "wind_gust_mph": round(wind_gust_mps * 2.237, 1) if wind_gust_mps else None,
        "wind_gust_mps": wind_gust_mps,
        "conditions": cc.get("conditions"),
    }


def fetch_nws_alerts():
    """Fetch active NWS alerts for the configured zone (Columbia County GA),
    matching dashboard_server.py exactly. Returns a list of dicts with
    event/severity/headline."""
    resp = requests.get(
        NWS_ALERTS_URL,
        headers={"User-Agent": NWS_USER_AGENT, "Accept": "application/json"},
        timeout=HTTP_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    data = resp.json()
    alerts = []
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        alerts.append({
            "event": props.get("event", ""),
            "severity": props.get("severity", ""),
            "headline": props.get("headline", "")[:120],
        })
    return alerts


# NWS event-name matching, ported exactly from dashboard_server.py
CRITICAL_EVENTS = [
    "Tornado Warning", "Tornado Emergency",
    "Severe Thunderstorm Warning", "Flash Flood Emergency",
    "Extreme Wind Warning",          # added 2026-10-19 (Dan)
]
WARNING_EVENTS = [
    "Severe Thunderstorm Watch", "Tornado Watch",
    "Flash Flood Warning", "Flash Flood Watch",
    "High Wind Warning",             # added 2026-10-19 (Dan)
]
# Matched as substrings of the NWS event name.
CAUTION_EVENTS = [
    "Thunderstorm", "Special Weather Statement",
    "Dense Fog Advisory",
]
# Matched only as the WHOLE event name (2026-10-19, Dan): "Wind Advisory"
# must not also match "Lake Wind Advisory", which is not a tower concern at
# this station (local gusts >= ~35 mph from the Tempest still raise CAUTION).
CAUTION_EVENTS_EXACT = ["Wind Advisory"]

_LEVEL_RANK = {"NONE": 0, "CAUTION": 1, "WARNING": 2, "CRITICAL": 3}


def nws_event_level(ev: str) -> str:
    """Tower level for one NWS event name."""
    if any(c in ev for c in CRITICAL_EVENTS):
        return "CRITICAL"
    if any(w in ev for w in WARNING_EVENTS):
        return "WARNING"
    if any(c in ev for c in CAUTION_EVENTS) or ev in CAUTION_EVENTS_EXACT:
        return "CAUTION"
    return "NONE"


def classify_tower_status(tempest: dict | None, nws_alerts: list) -> tuple[str, list]:
    """Combines Tempest readings + NWS alerts into a tower_status level
    (NONE/CAUTION/WARNING/CRITICAL) and a list of category-tagged alert
    dicts for the firmware. Logic ported exactly from dashboard_server.py's
    alert_monitor_loop -- same event-name matching, same lightning distance/
    recency thresholds, same wind gust threshold."""
    level = "NONE"
    alerts = []

    # Each NWS alert gets its own level; only matching alerts are listed, and
    # the overall level is the highest. (Before 2026-10-19 one running level
    # was reused, so after a first match every later NWS alert was listed
    # too, labelled with that running level.)
    for alert in nws_alerts:
        ev = alert["event"]
        ev_level = nws_event_level(ev)
        if ev_level == "NONE":
            continue
        if _LEVEL_RANK[ev_level] > _LEVEL_RANK[level]:
            level = ev_level
        alerts.append({
            "category": "tower",
            "level": ev_level,
            "message": alert.get("headline") or alert.get("event", "Weather alert"),
        })

    if tempest:
        lightning_km = tempest.get("lightning_km")
        lightning_ago_min = tempest.get("lightning_ago_min")
        lightning_1hr = tempest.get("lightning_1hr") or 0

        if level != "CRITICAL":
            if (lightning_km is not None and lightning_ago_min is not None
                    and lightning_ago_min <= LIGHTNING_RECENT_MINS):
                if lightning_km <= LIGHTNING_WARNING_KM:
                    if level != "WARNING":
                        level = "WARNING"
                    alerts.append({
                        "category": "tower",
                        "level": "WARNING",
                        "message": f"Lightning {lightning_km}km, {lightning_1hr} strikes/hr",
                    })
                elif lightning_km <= LIGHTNING_CAUTION_KM:
                    if level == "NONE":
                        level = "CAUTION"
                    alerts.append({
                        "category": "tower",
                        "level": "CAUTION",
                        "message": f"Lightning {lightning_km}km, {lightning_1hr} strikes/hr",
                    })

        wind_gust_mps = tempest.get("wind_gust_mps")
        if wind_gust_mps and wind_gust_mps >= WIND_GUST_CAUTION_MPS and level == "NONE":
            level = "CAUTION"
            alerts.append({
                "category": "tower",
                "level": "CAUTION",
                "message": f"Wind gust {tempest.get('wind_gust_mph')} mph",
            })

    if not alerts:
        alerts.append({"category": "tower", "level": "NONE", "message": "No active alerts"})

    return level, alerts


def weather_fetch_loop():
    """Background thread: refreshes tower/weather alerts every
    WEATHER_FETCH_INTERVAL_SEC seconds, forever. Mirrors
    dashboard_server.py's alert_monitor_loop polling pattern, but runs
    independently against the same two data sources."""
    while True:
        tempest = None
        nws_alerts = []
        any_success = False
        tempest_error = nws_error = None

        try:
            tempest = fetch_tempest()
            any_success = True
        except Exception as e:
            tempest_error = _redact(e)
            log.warning("Tempest fetch failed: %s", tempest_error)

        try:
            nws_alerts = fetch_nws_alerts()
            any_success = True
        except Exception as e:
            nws_error = str(e)
            log.warning("NWS alerts fetch failed: %s", e)

        if any_success:
            tower_status, tower_alerts = classify_tower_status(tempest, nws_alerts)
            with _cache_lock:
                existing_propagation_alerts = [
                    a for a in _cache["alerts"] if a.get("category") == "propagation"
                ]
                _cache["alerts"] = existing_propagation_alerts + tower_alerts
                _cache["tower_status"] = tower_status
                _weather.update({
                    "tempest": tempest, "nws_alerts": nws_alerts,
                    "updated": datetime.now(timezone.utc),
                    "tempest_error": tempest_error, "nws_error": nws_error,
                })
            log.info("Weather fetch OK: tower_status=%s", tower_status)
        # else: leave _cache["alerts"]/["tower_status"] untouched -- keep
        # last known-good tower data rather than wiping it on a transient
        # outage of both sources.

        time.sleep(WEATHER_FETCH_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/api/instrument/propagation", methods=["GET"])
def propagation_endpoint():
    with _cache_lock:
        solar = _cache["solar"]
        if solar.get("updated") is None:
            # No successful fetch yet since startup.
            return jsonify({"error": "no_data_yet"}), 503

        payload = {
            "updated": solar["updated"],
            "summary": solar.get("summary", "Unknown"),
            "sfi": solar.get("sfi"),
            "a_index": solar.get("a_index"),
            "k_index": solar.get("k_index"),
            "sunspots": solar.get("sunspots"),
            "xray": solar.get("xray"),
            "solar_wind": solar.get("solar_wind"),
            "bands": solar.get("bands", []),
            "alerts": _cache["alerts"],
            "tower_status": _cache["tower_status"],
        }
    return jsonify(payload)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


@app.route("/api/v2/conditions", methods=["GET"])
def conditions_v2():
    """Richer conditions for the dashboard and future displays (added
    2026-10-20, Roadmap Stage 2). Additive: /api/instrument/propagation (v1)
    is unchanged and remains the contract for the ESP32 and DXMon.

    Same band ratings as v1, plus each band's time of day, the geomagnetic
    penalty and recovery state, recent Kp, sun times at EM83, extra HamQSL
    fields (Bz, proton/electron flux, aurora, signal noise, geomagnetic field
    text), NOAA R/S/G scales with the 3-day forecast, and tower/weather
    detail (alert level, every NWS alert, Tempest readings)."""
    now = datetime.now(timezone.utc)
    with _cache_lock:
        solar = dict(_cache["solar"])
        alerts = list(_cache["alerts"])
        tower_status = _cache["tower_status"]
        engine = dict(_last_engine)
        weather = dict(_weather)
        kp_hist = list(_kp_history)
        kp_at = _kp_history_at
        scales = _noaa_scales
        scales_at = _noaa_scales_at
    if solar.get("updated") is None:
        return jsonify({"error": "no_data_yet"}), 503

    tods = engine.get("tods", {})
    storms = [t for t, kp in kp_hist if round(kp) >= RECOVERY_STORM_KP]
    recovery_ends = (max(storms) + timedelta(hours=RECOVERY_HOURS)) if storms else None
    s = _sun_times(now)
    tempest = weather.get("tempest") or {}

    payload = {
        "version": 2,
        "updated": solar["updated"],
        "source": solar.get("source"),
        "summary": solar.get("summary", "Unknown"),
        "solar": {
            "sfi": solar.get("sfi"),
            "sunspots": solar.get("sunspots"),
            "a_index": solar.get("a_index"),
            "k_index": solar.get("k_index"),
            "xray": solar.get("xray"),
            "solar_wind": solar.get("solar_wind"),
            **(solar.get("extra") or {}),
        },
        "geomagnetic": {
            "penalty": engine.get("penalty"),
            "recovery_active": bool(recovery_ends and recovery_ends > now),
            "recovery_ends": _iso(recovery_ends) if recovery_ends and recovery_ends > now else None,
            "kp_recent": [{"time": _iso(t), "kp": kp} for t, kp in kp_hist[-8:]],
            "kp_updated": _iso(kp_at),
        },
        "sun": {
            "civil_dawn": _iso(s["dawn"]), "sunrise": _iso(s["sunrise"]),
            "sunset": _iso(s["sunset"]), "civil_dusk": _iso(s["dusk"]),
        },
        "bands": [dict(b, tod=tods.get(b["band"])) for b in solar.get("bands", [])],
        "noaa_scales": dict(scales, updated=_iso(scales_at)) if scales else None,
        "tower": {
            "status": tower_status,
            "alerts": alerts,
            "nws_alerts": weather.get("nws_alerts", []),
            "lightning_km": tempest.get("lightning_km"),
            "lightning_ago_min": tempest.get("lightning_ago_min"),
            "lightning_1hr": tempest.get("lightning_1hr"),
            "wind_gust_mph": tempest.get("wind_gust_mph"),
            "conditions": tempest.get("conditions"),
            "updated": _iso(weather.get("updated")),
            "tempest_error": weather.get("tempest_error"),
            "nws_error": weather.get("nws_error"),
        },
    }
    return jsonify(payload)


@app.route("/healthz", methods=["GET"])
def health_check():
    """Simple liveness check -- distinct from data freshness. Returns 200
    as long as the Flask process is up, regardless of fetch success."""
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    solar_thread = threading.Thread(target=solar_fetch_loop, daemon=True)
    weather_thread = threading.Thread(target=weather_fetch_loop, daemon=True)
    solar_thread.start()
    weather_thread.start()

    log.info("PropMon starting on port %d", PORT)
    app.run(host="0.0.0.0", port=PORT)
