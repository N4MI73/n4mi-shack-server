"""
Propagation Recorder -- N4MI (temporary comparison tool)
=========================================================

Every 15 minutes it records, per band:
  - PropMon's rating and time of day      (GET <PROPMON_URL>/api/v2/conditions)
  - HamQSL's global day/night rating       (hamqsl.com/solarxml.php)
  - What was actually heard, from PSKReporter: every reception report made by
    receivers in grid EM83 during the last 15 minutes (one query), counted as
    unique senders per band in distance bands, plus N4MI's own reports.

It also keeps Dan's manual band sweeps (CW/SSB/FT8: DX / NA only / Nothing),
entered on its web page, so everything lands on one chart.

Purpose: decide, with evidence, whether PropMon's thresholds need tuning
(see the Propagation Engine Roadmap, "On-air check 2026-11-06"). It is meant to
run for a week or two and then be removed, or folded into PropMon later.

Design notes:
  - Python standard library only (no pip install), like the dashboard.
  - A failed source is stored as NULL, never as zero, so "no data" can't be
    mistaken for "band dead".
  - Distances are kept as a histogram per band (unique senders in 0-1000,
    1000-2000, 2000-4000, 4000-8000 and 8000+ km), so the "DX" line can be
    moved later without collecting again. The page treats 4000 km and beyond
    as DX by default: that leaves out the continental US, Canada, Mexico, the
    Caribbean and Central America, and includes Europe, Africa, Asia, most of
    South America, Alaska and Hawaii. (Tunable: DX_KM.)
  - PSKReporter asks for queries no more often than every 5 minutes; this
    makes one every 15. Set PSKR_APPCONTACT (an email) so its operator can
    reach you if he needs to -- optional, left blank by default.
  - LAN only, no login, like the other shack apps.
"""

import csv
import io
import json
import logging
import math
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree

# ---------------------------------------------------------------------------
# Settings (environment variables)
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("PORT", "8088"))
DB_PATH = os.environ.get("DB_PATH", "/data/proprecorder.db")
PROPMON_URL = os.environ.get("PROPMON_URL", "http://192.168.6.29:8076").rstrip("/")
GRID = os.environ.get("GRID", "EM83")
MYCALL = os.environ.get("MYCALL", "N4MI").upper()
DX_KM = int(os.environ.get("DX_KM", "4000"))
PSKR_APPCONTACT = os.environ.get("PSKR_APPCONTACT", "").strip()
PSKR_RPTLIMIT = int(os.environ.get("PSKR_RPTLIMIT", "10000"))
INTERVAL_MIN = 15
SETTLE_SEC = 60          # wait after each quarter hour so late reports arrive
HTTP_TIMEOUT = 30

HAMQSL_URL = "https://www.hamqsl.com/solarxml.php"
PSKR_URL = "https://retrieve.pskreporter.info/query"
USER_AGENT = "N4MI-PropRecorder/1.0"

BANDS = [  # name, low Hz, high Hz
    ("160m", 1_800_000, 2_000_000),
    ("80m", 3_500_000, 4_000_000),
    ("40m", 7_000_000, 7_300_000),
    ("30m", 10_100_000, 10_150_000),
    ("20m", 14_000_000, 14_350_000),
    ("17m", 18_068_000, 18_168_000),
    ("15m", 21_000_000, 21_450_000),
    ("12m", 24_890_000, 24_990_000),
    ("10m", 28_000_000, 29_700_000),
    ("6m", 50_000_000, 54_000_000),
]
BAND_NAMES = [b[0] for b in BANDS]
HAMQSL_GROUP = {"80m": "80m-40m", "40m": "80m-40m", "30m": "30m-20m", "20m": "30m-20m",
                "17m": "17m-15m", "15m": "17m-15m", "12m": "12m-10m", "10m": "12m-10m"}
DIST_BINS = [1000, 2000, 4000, 8000]   # km edges -> 5 bins: d0..d4
CW_MODES = {"CW"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("proprecorder")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    ts TEXT PRIMARY KEY,            -- end of the 15-min window, UTC ISO
    sfi INTEGER, k_index REAL, penalty REAL,
    propmon_source TEXT, propmon_updated TEXT, propmon_error TEXT,
    hamqsl_updated TEXT, hamqsl_error TEXT,
    sun_up INTEGER,                 -- 1 if the sun is up at EM83 (from PropMon)
    pskr_reports INTEGER, pskr_truncated INTEGER, pskr_error TEXT
);
CREATE TABLE IF NOT EXISTS band_slots (
    ts TEXT, band TEXT,
    propmon_status TEXT, propmon_tod TEXT,
    hamqsl_day TEXT, hamqsl_night TEXT,
    area_receivers INTEGER,         -- distinct EM83 receivers reporting this band
    area_senders INTEGER,           -- unique senders heard in EM83
    area_d0 INTEGER, area_d1 INTEGER, area_d2 INTEGER, area_d3 INTEGER, area_d4 INTEGER,
    area_maxkm INTEGER,
    area_cw INTEGER,                -- unique senders heard on CW
    own_senders INTEGER,            -- unique senders heard by MYCALL
    own_dx INTEGER,                 -- ... at DX_KM or more (at the time recorded)
    own_maxkm INTEGER,
    PRIMARY KEY (ts, band)
);
CREATE TABLE IF NOT EXISTS sweeps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,               -- UTC ISO, when Dan listened
    band TEXT NOT NULL, mode TEXT NOT NULL, heard TEXT NOT NULL,
    region TEXT, antenna TEXT, note TEXT,
    created TEXT NOT NULL
);
"""

_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _db_lock, db() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_get(url, params=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def short_error(e):
    # Never include URLs with query strings (appcontact) in stored errors.
    return f"{type(e).__name__}: {str(e)[:120]}".replace(PSKR_APPCONTACT or "\0", "***")


def grid_to_latlon(loc):
    """Centre of a Maidenhead locator (4, 6 or 8 characters). None if invalid."""
    if not loc:
        return None
    loc = loc.strip().upper()
    if len(loc) < 4 or len(loc) % 2:
        return None
    try:
        lon = (ord(loc[0]) - 65) * 20 - 180
        lat = (ord(loc[1]) - 65) * 10 - 90
        lon += int(loc[2]) * 2
        lat += int(loc[3])
        w, h = 2.0, 1.0
        if len(loc) >= 6:
            w, h = w / 24, h / 24
            lon += (ord(loc[4]) - 65) * w
            lat += (ord(loc[5]) - 65) * h
        if len(loc) >= 8:
            w, h = w / 10, h / 10
            lon += int(loc[6]) * w
            lat += int(loc[7]) * h
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            return None
        return lat + h / 2, lon + w / 2
    except (ValueError, IndexError):
        return None


def km_between(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def band_of(freq_hz):
    for name, lo, hi in BANDS:
        if lo <= freq_hz <= hi:
            return name
    return None


def dist_bin(km):
    for i, edge in enumerate(DIST_BINS):
        if km < edge:
            return i
    return len(DIST_BINS)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def fetch_propmon():
    data = json.loads(http_get(PROPMON_URL + "/api/v2/conditions"))
    if "bands" not in data:
        raise ValueError(data.get("error", "no bands in response"))
    return data


def parse_hamqsl(xml_bytes):
    """HamQSL calculated conditions: {group: {"day": "Good", "night": "Poor"}}."""
    root = ElementTree.fromstring(xml_bytes)
    sd = root.find("solardata")
    if sd is None:
        raise ValueError("no <solardata>")
    out = {}
    cc = sd.find("calculatedconditions")
    for b in (cc if cc is not None else []):
        name, when = b.get("name"), (b.get("time") or "").lower()
        if name and when in ("day", "night") and b.text:
            out.setdefault(name, {})[when] = b.text.strip().lower()
    upd = sd.findtext("updated")
    return out, (upd.strip() if upd else None)


def fetch_pskreporter(window_start, window_end):
    """One query: every reception report by receivers in GRID, covering the
    window. Returns (per-band aggregates, report count, truncated flag)."""
    lookback = int((datetime.now(timezone.utc) - window_start).total_seconds()) + 120
    params = {
        "receiverCallsign": GRID, "modify": "grid",
        "flowStartSeconds": -min(lookback, 86400),
        "rronly": 1, "rptlimit": PSKR_RPTLIMIT,
    }
    if PSKR_APPCONTACT:
        params["appcontact"] = PSKR_APPCONTACT
    root = ElementTree.fromstring(http_get(PSKR_URL, params))
    return aggregate_reports(root, window_start, window_end)


def aggregate_reports(root, window_start, window_end):
    t0, t1 = window_start.timestamp(), window_end.timestamp()
    reports = root.findall("receptionReport")
    truncated = len(reports) >= PSKR_RPTLIMIT
    agg = {b: {"receivers": set(), "senders": {}, "cw": set(),
               "own": {}} for b in BAND_NAMES}
    used = 0
    for r in reports:
        try:
            ts = int(r.get("flowStartSeconds", "0"))
            freq = int(float(r.get("frequency", "0")))
        except ValueError:
            continue
        if not (t0 <= ts < t1):
            continue
        band = band_of(freq)
        rx_loc = (r.get("receiverLocator") or "").upper()
        if band is None or not rx_loc.startswith(GRID.upper()):
            continue
        sender = (r.get("senderCallsign") or "").upper()
        receiver = (r.get("receiverCallsign") or "").upper()
        if not sender:
            continue
        used += 1
        a, b = grid_to_latlon(rx_loc), grid_to_latlon(r.get("senderLocator"))
        km = km_between(a, b) if (a and b) else None
        slot = agg[band]
        slot["receivers"].add(receiver)
        # Keep the longest distance seen for each sender.
        prev = slot["senders"].get(sender)
        if prev is None or (km is not None and (prev < 0 or km > prev)):
            slot["senders"][sender] = km if km is not None else -1
        if (r.get("mode") or "").upper() in CW_MODES:
            slot["cw"].add(sender)
        if receiver.split("/")[0] == MYCALL or receiver == MYCALL:
            prev = slot["own"].get(sender)
            if prev is None or (km is not None and (prev < 0 or km > prev)):
                slot["own"][sender] = km if km is not None else -1
    out = {}
    for band, slot in agg.items():
        bins = [0] * (len(DIST_BINS) + 1)
        for km in slot["senders"].values():
            if km >= 0:
                bins[dist_bin(km)] += 1
        own_km = [k for k in slot["own"].values() if k >= 0]
        out[band] = {
            "area_receivers": len(slot["receivers"]),
            "area_senders": len(slot["senders"]),
            "bins": bins,
            "area_maxkm": int(max([k for k in slot["senders"].values() if k >= 0], default=0)),
            "area_cw": len(slot["cw"]),
            "own_senders": len(slot["own"]),
            "own_dx": sum(1 for k in own_km if k >= DX_KM),
            "own_maxkm": int(max(own_km, default=0)),
        }
    return out, used, truncated


# ---------------------------------------------------------------------------
# One recording cycle
# ---------------------------------------------------------------------------

def record_slot(slot_end):
    window_start = slot_end - timedelta(minutes=INTERVAL_MIN)
    row = {"ts": iso(slot_end)}
    bands = {b: {} for b in BAND_NAMES}

    try:
        pm = fetch_propmon()
        solar = pm.get("solar") or {}
        row.update(sfi=solar.get("sfi"), k_index=solar.get("k_index"),
                   penalty=(pm.get("geomagnetic") or {}).get("penalty"),
                   propmon_source=pm.get("source"), propmon_updated=pm.get("updated"))
        usable = pm.get("source") != "noaa" and pm.get("summary") != "Data unavailable"
        for b in pm.get("bands", []):
            if b.get("band") in bands and usable:
                bands[b["band"]].update(propmon_status=b.get("status"), propmon_tod=b.get("tod"))
        if not usable:
            row["propmon_error"] = "PropMon on NOAA fallback: ratings unavailable"
        sun = pm.get("sun") or {}
        if sun.get("sunrise") and sun.get("sunset"):
            row["sun_up"] = int(sun["sunrise"] <= iso(slot_end) < sun["sunset"])
    except Exception as e:
        row["propmon_error"] = short_error(e)
        log.warning("PropMon fetch failed: %s", e)

    try:
        groups, upd = parse_hamqsl(http_get(HAMQSL_URL))
        row["hamqsl_updated"] = upd
        for b, g in HAMQSL_GROUP.items():
            if g in groups:
                bands[b].update(hamqsl_day=groups[g].get("day"), hamqsl_night=groups[g].get("night"))
    except Exception as e:
        row["hamqsl_error"] = short_error(e)
        log.warning("HamQSL fetch failed: %s", e)

    try:
        agg, used, truncated = fetch_pskreporter(window_start, slot_end)
        row.update(pskr_reports=used, pskr_truncated=int(truncated))
        for b, a in agg.items():
            bands[b].update(
                area_receivers=a["area_receivers"], area_senders=a["area_senders"],
                area_d0=a["bins"][0], area_d1=a["bins"][1], area_d2=a["bins"][2],
                area_d3=a["bins"][3], area_d4=a["bins"][4], area_maxkm=a["area_maxkm"],
                area_cw=a["area_cw"], own_senders=a["own_senders"],
                own_dx=a["own_dx"], own_maxkm=a["own_maxkm"])
    except Exception as e:
        row["pskr_error"] = short_error(e)
        log.warning("PSKReporter fetch failed: %s", short_error(e))

    with _db_lock, db() as conn:
        cols = list(row)
        conn.execute(f"INSERT OR REPLACE INTO slots ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     [row[c] for c in cols])
        for b, vals in bands.items():
            vals = dict(vals, ts=row["ts"], band=b)
            cols = list(vals)
            conn.execute(f"INSERT OR REPLACE INTO band_slots ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         [vals[c] for c in cols])
    log.info("Recorded %s: SFI=%s K=%s pskr=%s%s", row["ts"], row.get("sfi"), row.get("k_index"),
             row.get("pskr_reports"), " (truncated)" if row.get("pskr_truncated") else "")


def next_slot_end(now):
    minute = (now.minute // INTERVAL_MIN + 1) * INTERVAL_MIN
    base = now.replace(minute=0, second=0, microsecond=0)
    return base + timedelta(minutes=minute)


def recorder_loop():
    while True:
        now = datetime.now(timezone.utc)
        slot_end = next_slot_end(now)
        wait = (slot_end - now).total_seconds() + SETTLE_SEC
        time.sleep(max(1, wait))
        try:
            record_slot(slot_end)
        except Exception:
            log.exception("Recording cycle failed")


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
SWEEP_MODES = {"SSB", "FT8", "CW"}   # form offers SSB and FT8 (Dan, 2026-11-07); CW still accepted
SWEEP_HEARD = {"DX", "NA only", "Nothing"}


def query_data(days):
    since = iso(datetime.now(timezone.utc) - timedelta(days=days))
    with db() as conn:
        slots = [dict(r) for r in conn.execute("SELECT * FROM slots WHERE ts >= ? ORDER BY ts", (since,))]
        bands = [dict(r) for r in conn.execute("SELECT * FROM band_slots WHERE ts >= ? ORDER BY ts", (since,))]
        sweeps = [dict(r) for r in conn.execute("SELECT * FROM sweeps WHERE ts >= ? ORDER BY ts", (since,))]
    return {"slots": slots, "bands": bands, "sweeps": sweeps,
            "settings": {"grid": GRID, "mycall": MYCALL, "dx_km": DX_KM,
                         "dist_bins": DIST_BINS, "band_names": BAND_NAMES}}


def export_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    with db() as conn:
        rows = conn.execute(
            "SELECT b.*, s.sfi, s.k_index, s.penalty, s.sun_up, s.propmon_error, s.hamqsl_error, "
            "s.pskr_error, s.pskr_truncated FROM band_slots b JOIN slots s ON s.ts = b.ts ORDER BY b.ts, b.band")
        w.writerow([d[0] for d in rows.description])
        w.writerows(rows)
        w.writerow([])
        rows = conn.execute("SELECT * FROM sweeps ORDER BY ts")
        w.writerow(["sweeps"] + [d[0] for d in rows.description])
        w.writerows([[""] + list(r) for r in rows])
    return buf.getvalue()


def validate_sweep(body):
    try:
        ts = datetime.fromisoformat(str(body.get("ts", "")).replace("Z", "+00:00"))
    except ValueError:
        return None, "time must be an ISO date-time"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    band, mode, heard = body.get("band"), body.get("mode"), body.get("heard")
    if band not in BAND_NAMES:
        return None, "unknown band"
    if mode not in SWEEP_MODES:
        return None, "mode must be SSB or FT8"
    if mode == "SSB" and band == "30m":
        return None, "no phone on 30m"
    if heard not in SWEEP_HEARD:
        return None, "heard must be DX, NA only or Nothing"
    def text(k, n):
        return str(body.get(k) or "")[:n].strip() or None
    return {"ts": iso(ts), "band": band, "mode": mode, "heard": heard,
            "region": text("region", 60), "antenna": text("antenna", 30), "note": text("note", 200),
            "created": iso(datetime.now(timezone.utc))}, None


class Handler(BaseHTTPRequestHandler):
    server_version = "PropRecorder/1.0"

    def log_message(self, fmt, *args):
        pass  # keep the container log for recording cycles

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html")
        if url.path == "/api/data":
            try:
                days = max(1, min(30, int(qs.get("days", ["3"])[0])))
            except ValueError:
                days = 3
            return self._send(200, query_data(days))
        if url.path == "/api/export.csv":
            data = export_csv()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=proprecorder.csv")
            self.end_headers()
            return self.wfile.write(data.encode())
        if url.path == "/healthz":
            return self._send(200, {"status": "ok"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/sweeps":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 10_000:
                return self._send(413, {"error": "too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
            if isinstance(body, dict):
                body = [body]
            rows = []
            for item in body[:20]:
                row, err = validate_sweep(item if isinstance(item, dict) else {})
                if err:
                    return self._send(400, {"error": err})
                rows.append(row)
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "bad JSON"})
        with _db_lock, db() as conn:
            for row in rows:
                cols = list(row)
                conn.execute(f"INSERT INTO sweeps ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                             [row[c] for c in cols])
        self._send(200, {"saved": len(rows)})

    def do_DELETE(self):
        parts = self.path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "sweeps"] and parts[2].isdigit():
            with _db_lock, db() as conn:
                n = conn.execute("DELETE FROM sweeps WHERE id = ?", (int(parts[2]),)).rowcount
            return self._send(200, {"deleted": n})
        self._send(404, {"error": "not found"})


def main():
    init_db()
    threading.Thread(target=recorder_loop, daemon=True).start()
    server = ThreadingHTTPServer(("", PORT), Handler)
    log.info("Propagation Recorder on port %d; PropMon %s; grid %s; DX >= %d km",
             PORT, PROPMON_URL, GRID, DX_KM)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
