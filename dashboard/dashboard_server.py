#!/usr/bin/env python3
r"""
Ham Radio Dashboard Server
Runs on n4misrv as a Docker container (see dashboard/docker-compose.yml).
(Windows cold spare: C:\Ham Scripts\dashboard_server.py -- an older version
that still polls HamQSL/Tempest/NWS itself.)

Since 2026-11-07 (Propagation Engine Roadmap, Stage 3) this server does no
propagation or weather work of its own. PropMon on the NAS is the single
engine: this server relays PropMon's /api/v2/conditions to the page and builds
/api/storm from PropMon's tower data. No Tempest token is needed here any more.

Endpoints:
  /N4MI_PropagationDashboard.html   the page
  /api/conditions   PropMon's v2 conditions, relayed (502 if PropMon is down)
  /api/storm        storm/tower status in the dashboard's original JSON shape
                    (an external contract; Uptime Kuma checks it)
  /api/adxo         DXMon's active-DXpedition feed, relayed
"""

import http.server
import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone

PORT       = 8073
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PAGE       = "N4MI_PropagationDashboard.html"

# PropMon on the NAS -- the propagation and tower-alert engine.
PROPMON_URL = os.environ.get("PROPMON_URL", "http://192.168.6.29:8076").rstrip("/")

# ── DXMon ADXO feed (Desktop Instrument Series project, NAS-hosted) ──
# Undocumented by design in both projects' READMEs -- internal use only,
# per the confirmed handoff note (2026-09-10). Do not publicize this path.
DXMON_ADXO_URL = "http://192.168.6.29:8083/api/adxo"

CACHE_SECONDS = 60      # PropMon itself refreshes every 2-5 minutes
HTTP_TIMEOUT  = 10

# Last good PropMon response, shared by /api/conditions and /api/storm.
_cache = {"data": None, "fetched": 0.0, "error": None, "tried": 0.0}
_cache_lock = threading.Lock()


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "N4MI-Dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


def get_conditions():
    """Returns (data, error). data is the last good PropMon v2 response (or
    None if there has never been one); error is set when the latest attempt
    failed. Fetches at most once per CACHE_SECONDS."""
    with _cache_lock:
        now = time.time()
        if now - _cache["tried"] >= CACHE_SECONDS:
            _cache["tried"] = now
            try:
                data = fetch_json(PROPMON_URL + "/api/v2/conditions")
                if "bands" not in data:
                    raise ValueError(data.get("error") or "unexpected response")
                _cache.update(data=data, fetched=now, error=None)
            except Exception as e:
                _cache["error"] = f"PropMon not answering ({type(e).__name__})"
                print(f"[PROPMON] {e}")
        return _cache["data"], _cache["error"]


def storm_from_propmon(data, error):
    """/api/storm in its original shape (unchanged since 2026-10-17), filled
    from PropMon's v2 tower section. If PropMon can't be reached, the last
    good values are kept and "error" says so -- the same behavior as the old
    built-in storm monitor on a failure."""
    state = {
        "level": "NONE", "nws_alerts": [], "lightning_km": None, "lightning_ago": None,
        "lightning_1hr": 0, "conditions": "Unknown", "wind_gust": None,
        "last_updated": None, "error": error,
    }
    tower = (data or {}).get("tower") or {}
    if tower:
        updated = tower.get("updated")
        try:
            last = datetime.fromisoformat(updated.replace("Z", "+00:00")).strftime("%H:%M UTC")
        except (AttributeError, ValueError):
            last = None
        state.update({
            "level":         tower.get("status") or "NONE",
            "nws_alerts":    [{"event": a.get("event", ""), "severity": a.get("severity", ""),
                               "headline": a.get("headline", "")}
                              for a in (tower.get("nws_alerts") or [])],
            "lightning_km":  tower.get("lightning_km"),
            "lightning_ago": tower.get("lightning_ago_min"),
            "lightning_1hr": tower.get("lightning_1hr") or 0,
            "conditions":    tower.get("conditions") or ("Error" if tower.get("tempest_error") else "Unknown"),
            "wind_gust":     tower.get("wind_gust_mph") or None,   # 0 shown as none, as before
            "last_updated":  last,
        })
    elif error is None:
        state["error"] = "No tower data from PropMon yet"
    return state


# ── HTTP request handler ──────────────────────────────────
class DashboardHandler(http.server.SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SCRIPT_DIR, **kwargs)

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/conditions":
            data, error = get_conditions()
            if error or data is None:
                return self._json(502, {"error": error or "No data from PropMon yet"})
            return self._json(200, data)

        if path == "/api/storm":
            data, error = get_conditions()
            return self._json(200, storm_from_propmon(data, error))

        if path == "/api/adxo":
            try:
                return self._json(200, fetch_json(DXMON_ADXO_URL))
            except Exception as e:
                return self._json(500, {"error": type(e).__name__})

        # Serve only the page itself (and / as a shortcut to it).
        if path in ("/", "/" + PAGE):
            self.path = "/" + PAGE
            self._page = True
            return super().do_GET()
        self.send_error(404)

    def end_headers(self):
        # Make browsers check for a newer page every time (a cheap 304 when
        # unchanged), so an update shows without Ctrl+F5 (2026-11-07: cached
        # old pages showed ERROR/LOADING against the new server).
        if getattr(self, "_page", False):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, format, *args):
        if "/api/" in str(args[0]):
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] {args[0]}")


# ── Main ──────────────────────────────────────────────────
if __name__ == "__main__":
    print()
    print(f"  N4MI Ham Radio Dashboard Server")
    print(f"  Propagation + storm data from PropMon: {PROPMON_URL}")
    print(f"  Dashboard:     http://localhost:{PORT}/{PAGE}")
    print(f"  Storm API:     http://localhost:{PORT}/api/storm")
    print()

    with http.server.ThreadingHTTPServer(("", PORT), DashboardHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            print("\n  Server stopped. 73!")
