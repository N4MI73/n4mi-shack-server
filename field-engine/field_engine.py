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
"""

import json
import math
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import elevation, sun

BAND_ORDER = ["160m", "80m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"]
_HERE = os.path.dirname(os.path.abspath(__file__))


def load_model(path=None):
    """Load and check rating_model.json. Raises ValueError if it is unusable."""
    path = path or os.path.join(_HERE, "rating_model.json")
    with open(path, encoding="utf-8") as f:
        m = json.load(f)
    tod = m["time_of_day"]
    switch = set(tod["civil_twilight_bands"]) | set(tod["sunset_bands"])
    for band in BAND_ORDER:
        want = {"day", "night"} if band in switch else {"night", "dawn", "day", "dusk"}
        periods = m["bands"].get(band, {})
        if set(periods) != want:
            raise ValueError(f"{path}: band {band} periods must be {sorted(want)}")
        for pair in periods.values():
            if (len(pair) != 2 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in pair)
                    or pair[0] > pair[1]):
                raise ValueError(f"{path}: band {band} needs [fair, good] with fair <= good")
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


def rate_band(band, sfi, penalty, tod, model):
    fair, good = model["bands"][band][tod]
    score = sfi * penalty
    return "good" if score >= good else "fair" if score >= fair else "poor"


def rate_all(sfi, k_index, kp_history, when_utc, lat, lon, tz, model):
    """All 10 bands. Returns {"model_version", "available", "penalty",
    "bands": [{"band", "status", "tod"}, ...]}; "available" is False (and
    "bands" empty) when sfi or k_index is missing."""
    if when_utc.tzinfo is None:
        raise ValueError("when_utc must be timezone-aware (UTC)")
    if sfi is None or k_index is None:
        return {"model_version": model["model_version"], "available": False, "penalty": None, "bands": []}
    penalty = effective_penalty(k_index, kp_history or [], when_utc, model)
    out = []
    for band in BAND_ORDER:
        tod = time_of_day(band, when_utc, lat, lon, tz, model)
        out.append({"band": band, "status": rate_band(band, sfi, penalty, tod, model), "tod": tod})
    return {"model_version": model["model_version"], "available": True, "penalty": penalty, "bands": out}


if __name__ == "__main__":   # quick look: EM83, now, SFI 122, K1
    m = load_model()
    r = rate_all(122, 1, [], datetime.now(timezone.utc), 33.45, -82.19, "America/New_York", m)
    print(m["model_version"], r["penalty"])
    for b in r["bands"]:
        print(f'{b["band"]:>5} {b["tod"]:>6} {b["status"]}')
