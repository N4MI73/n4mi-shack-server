"""
PropMon Field Engine -- the band-rating engine for running away from home.

Reference implementation for the Ham Ops Console's Field Mode (Ham Shack
Automation, 2026-11-17). It rates the same 10 bands with the same model as
PropMon (NAS :8076), but for any location, so the time of day follows the
sun where the station actually is.

    score = SFI x geomagnetic penalty
    band rating = good / fair / poor against that band's [fair, good]
                  thresholds for its current time of day

The numbers live in rating_model.json (same file as PropMon's, same
model_version). Do not edit them here: Ham Shack Automation tunes the model
and bumps model_version. At home, compare this file's model_version with
PropMon v2's "model_version" and take the new file when they differ.

Dependencies: Python 3.9+ and astral (pip install astral==3.2).
At EM83 (33.45, -82.19, America/New_York) the results equal PropMon's
exactly -- see test_vectors.json and test_field_engine.py.

Inputs the field station must supply (PropMon gets them from HamQSL/NOAA):
  sfi         10.7 cm solar flux (HamQSL "solarflux", or NOAA)
  k_index     current planetary K (HamQSL "kindex", or NOAA)
  kp_history  recent 3-hour planetary Kp readings [(datetime_utc, kp), ...]
              for the storm-recovery rule (NOAA noaa-planetary-k-index.json);
              pass [] if not available -- then no recovery hold is applied
If sfi or k_index is missing, rate_all() returns no ratings ("unavailable"),
never ten "poor" bands.

Two scales (pick with rate_all(..., scale=...)):
  "dx"         (default) PropMon's scale -- GOOD = DX on SSB, FAIR = DX on FT8
               only or North America only on SSB, POOR = less. All 10 bands.
  "field_day"  North America reach on the non-WARC Field Day bands
               (160/80/40/20/15/10/6m) -- meanings in model["field_day"]
               ["meanings"]. Own thresholds and its own "version" in the
               "field_day" block; it does not change model_version.
"""

import json
import math
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import elevation, sun

BAND_ORDER = ["160m", "80m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"]
FIELD_DAY_BANDS = ["160m", "80m", "40m", "20m", "15m", "10m", "6m"]
STATUSES = ("good", "fair", "poor")
_HERE = os.path.dirname(os.path.abspath(__file__))


def _check_bands(path, label, bands, names, switch):
    if not isinstance(bands, dict):
        raise ValueError(f"{path}: {label} bands must be an object")
    for band in names:
        periods = bands.get(band, {})
        if not isinstance(periods, dict):
            raise ValueError(f"{path}: {label} band {band} must be an object")
        if "fixed" in periods:   # field_day only: one status whatever the conditions
            if (label != "field_day" or periods["fixed"] not in STATUSES
                    or not set(periods) <= {"fixed", "note"}
                    or not isinstance(periods.get("note", ""), str)):
                raise ValueError(f"{path}: {label} band {band}: 'fixed' (good/fair/poor, optional text "
                                 f"'note', nothing else) is allowed only in the field_day scale")
            continue
        want = {"day", "night"} if band in switch else {"night", "dawn", "day", "dusk"}
        if set(periods) != want:
            raise ValueError(f"{path}: {label} band {band} periods must be {sorted(want)}")
        for pair in periods.values():
            if (not isinstance(pair, list) or len(pair) != 2
                    or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in pair)
                    or pair[0] > pair[1]):
                raise ValueError(f"{path}: {label} band {band} needs [fair, good] with fair <= good")


def load_model(path=None):
    """Load and check rating_model.json. Raises ValueError if it is unusable."""
    path = path or os.path.join(_HERE, "rating_model.json")
    with open(path, encoding="utf-8") as f:
        m = json.load(f)
    tod = m["time_of_day"]
    switch = set(tod["civil_twilight_bands"]) | set(tod["sunset_bands"])
    _check_bands(path, "dx", m["bands"], BAND_ORDER, switch)
    fd = m.get("field_day")
    if fd is not None:
        if not isinstance(fd, dict) or not isinstance(fd.get("meanings"), dict):
            raise ValueError(f"{path}: field_day must be an object with version, meanings and bands")
        if not fd.get("version"):
            raise ValueError(f"{path}: field_day.version missing")
        if set(fd.get("meanings", {})) != set(STATUSES):
            raise ValueError(f"{path}: field_day.meanings needs good, fair and poor")
        if set(fd.get("bands", {})) != set(FIELD_DAY_BANDS):
            raise ValueError(f"{path}: field_day bands must be {FIELD_DAY_BANDS}")
        _check_bands(path, "field_day", fd["bands"], FIELD_DAY_BANDS, switch)
    tiers = [m["geomagnetic"]["k_tiers"][str(k)] for k in range(8)]
    if any(not 0 < t <= 1 for t in tiers) or any(a < b for a, b in zip(tiers, tiers[1:])):
        raise ValueError(f"{path}: k_tiers must be in (0, 1] and never rise with K")
    if not m.get("model_version"):
        raise ValueError(f"{path}: model_version missing")
    return m


def _sun_events(when_utc, lat, lon, tz):
    """dawn/sunrise/sunset/dusk for the local date at the location, or None
    where the sun doesn't rise/set that day (very high latitudes)."""
    try:
        local_date = when_utc.astimezone(tz).date()
        return sun(Observer(lat, lon), date=local_date, tzinfo=tz)
    except ValueError:
        return None


def time_of_day(band, when_utc, lat, lon, tz, model):
    """'day' / 'night' for 160m and 80m; 'dawn' / 'day' / 'dusk' / 'night'
    for 40m-6m -- the same rules as PropMon, at this location."""
    if isinstance(tz, str):
        tz = ZoneInfo(tz)
    t = model["time_of_day"]
    s = _sun_events(when_utc, lat, lon, tz)
    now = when_utc.astimezone(tz)
    if s is None:   # polar day or night: decide by the sun's height alone
        return "day" if elevation(Observer(lat, lon), when_utc) > 0 else "night"
    if band in t["civil_twilight_bands"]:
        return "night" if (now < s["dawn"] or now >= s["dusk"]) else "day"
    if band in t["sunset_bands"]:
        return "night" if (now < s["sunrise"] or now >= s["sunset"]) else "day"
    w = timedelta(hours=t["dawn_dusk_half_width_hours"])
    if s["sunrise"] - w <= now < s["sunrise"] + w:
        return "dawn"
    if s["sunset"] - w <= now < s["sunset"] + w:
        return "dusk"
    if s["sunrise"] + w <= now < s["sunset"] - w:
        return "day"
    return "night"


def geo_penalty(k_index, model):
    """Penalty for the current K (fractional Kp rounded, K above 7 treated as 7)."""
    k = min(max(int(round(k_index)), 0), 7)
    return float(model["geomagnetic"]["k_tiers"][str(k)])


def effective_penalty(k_index, kp_history, when_utc, model):
    """Current-K penalty, held at no better than the recovery cap for
    recovery_hours after any 3-hour Kp at storm level."""
    g = model["geomagnetic"]
    p = geo_penalty(k_index, model)
    cutoff = when_utc - timedelta(hours=g["recovery_hours"])
    if any(t >= cutoff and round(kp) >= g["recovery_storm_kp"] for t, kp in kp_history):
        p = min(p, g["recovery_penalty_cap"])
    return p


def _scale(model, scale):
    if scale == "dx":
        return model["bands"], BAND_ORDER, model["model_version"]
    if scale == "field_day":
        fd = model.get("field_day")
        if fd is None:
            raise ValueError("this rating_model.json has no field_day scale")
        return fd["bands"], FIELD_DAY_BANDS, fd["version"]
    raise ValueError(f"unknown scale {scale!r} (use 'dx' or 'field_day')")


def rate_band(band, sfi, penalty, tod, model, scale="dx"):
    bands = _scale(model, scale)[0]
    if "fixed" in bands[band]:
        return bands[band]["fixed"]
    fair, good = bands[band][tod]
    score = sfi * penalty
    return "good" if score >= good else "fair" if score >= fair else "poor"


def rate_all(sfi, k_index, kp_history, when_utc, lat, lon, tz, model, scale="dx"):
    """Rate every band of the chosen scale ("dx": all 10 bands; "field_day":
    160/80/40/20/15/10/6m). Returns {"model_version", "scale",
    "scale_version", "available", "penalty", "bands": [{"band", "status",
    "tod"}, ...]}; a band with a fixed status (field_day 6m) also carries
    "note". "available" is False (and "bands" empty) when sfi or k_index is
    missing."""
    if when_utc.tzinfo is None:
        raise ValueError("when_utc must be timezone-aware (UTC)")
    bands, names, scale_version = _scale(model, scale)
    head = {"model_version": model["model_version"], "scale": scale, "scale_version": scale_version}
    if sfi is None or k_index is None:
        return {**head, "available": False, "penalty": None, "bands": []}
    penalty = effective_penalty(k_index, kp_history or [], when_utc, model)
    out = []
    for band in names:
        tod = time_of_day(band, when_utc, lat, lon, tz, model)
        entry = {"band": band, "status": rate_band(band, sfi, penalty, tod, model, scale), "tod": tod}
        if "note" in bands[band]:
            entry["note"] = bands[band]["note"]
        out.append(entry)
    return {**head, "available": True, "penalty": penalty, "bands": out}


if __name__ == "__main__":   # quick look: EM83, now, SFI 122, K1, both scales
    m = load_model()
    for scale in ("dx", "field_day"):
        r = rate_all(122, 1, [], datetime.now(timezone.utc), 33.45, -82.19, "America/New_York", m, scale)
        print(scale, r["scale_version"], r["penalty"])
        for b in r["bands"]:
            print(f'{b["band"]:>5} {b["tod"]:>6} {b["status"]}')
