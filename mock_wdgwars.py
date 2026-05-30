#!/usr/bin/env python3
"""mock_wdgwars: a local HTTP server that mimics wdgwars.pl's probe surface.

Used by integration_test.py so the test suite doesn't hammer the real API.
Also useful as a standalone learning tool — point the tester at this mock
and see how each scenario maps to a verdict surface.

Four scenarios:

  outage    — the current real state: every /api/* returns the styled 404
              page, /api/stats 200s with LSWS admin telemetry. Probe verdict
              surface should be DEGRADED+LEAK (or OUTAGE+LEAK with valid key).
  healthy   — API is up: /api/me returns 200 with identity, /api/stats is
              explicitly blocked (404). Probe surface should be HEALTHY.
  partial   — API is up but /api/stats is still leaking. Probe surface
              should be HEALTHY+LEAK.
  diverged  — sentinel paths under /api/<random> return distinct bodies,
              breaking the quorum. Probe surface should carry
              +SENTINEL-DIVERGED.

Stand it up:

    python3 mock_wdgwars.py --scenario outage --port 9999
    # then in another shell:
    python3 wdgwars_api_tester.py --hosts http://127.0.0.1:9999 \\
        --variants none,garbage
"""
from __future__ import annotations

import argparse
import http.server
import json
import random
import string
import sys
import threading
from typing import Optional

# ────────────────────────── Canned response bodies ───────────────────────────

# The styled /api/* "upstream not bound" 404 page that real wdgwars.pl serves
# during the current outage. Constant body so the quorum sentinel converges.
STYLED_404_BODY = (b"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>404 Not Found</title>
<style>body{font-family:system-ui,sans-serif;background:#1a1a1a;color:#eee;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{text-align:center}.code{color:#e74c3c;font-size:6rem;font-weight:700;
margin:0}.msg{color:#999;margin-top:1rem}</style></head><body>
<div class="box"><p class="code">404</p><p class="msg">Not Found</p></div>
</body></html>
""")

# Django's bare 404 template — what real wdgwars.pl serves for paths outside
# /api/*. Smaller, no inline CSS, has a Cloudflare beacon comment.
BARE_404_BODY = b"<h1>404 Not Found</h1><!-- cf-beacon -->"

# /api/stats LSWS admin telemetry leak body (from the real bug report).
STATS_LEAK_BODY = json.dumps({
    "uptime": 957,
    "version": "1.3.0",
    "requests": {"total": 10323, "per_second": 10.8},
    "bytes": {"total": 418666149},
    "status": {"200": 6398, "404": 2531, "500": 60},
    "cache": {"hits": 71, "misses": 5808, "hit_rate": 1.21},
    "shield": {"coalesced": 1, "stale_budget": 0, "jailed": 18},
    "connections": {"active": 21},
    "php": {"lsphp_processes": 0},
    "memory_kb": 85272,
    "top_domains": [
        {"domain": "www.sklep-tvsat.com", "requests": 2186},
        {"domain": "www.wdgwars.pl", "requests": 2030},
        {"domain": "www.foto.kronikawypraw.eu", "requests": 1996},
    ],
}, indent=2).encode()

# Healthy API responses.
ME_OK_BODY = json.dumps({
    "user": "alleycat",
    "uploads": 36042,
    "badges": ["wardriver", "mesh_first", "adsb_century"],
    "gang": "JCMK",
}).encode()

UPLOAD_HISTORY_OK_BODY = json.dumps({
    "count": 10,
    "rows": [
        {"endpoint": "upload", "timestamp": "2026-05-29T12:00:00Z",
         "imported": 42},
    ] * 10,
}).encode()

UPLOAD_CSV_OK_BODY = json.dumps({
    "imported": 2,
    "captured": 2,
    "duplicates": 0,
    "no_gps": 0,
    "bad_rows": 0,
    "merged_samples": 0,
}).encode()

CHANGELOG_BODY = (b"<!doctype html><html><body><h1>Changelog</h1>"
                   b"<p>Mock changelog page for testing.</p></body></html>")


# ───────────────────────── Scenario behavior ─────────────────────────────────


def _div_body(seed: str) -> bytes:
    """Generate a slightly-different styled 404 body. Used by `diverged`
    scenario to break the quorum (every sentinel path gets a unique body).
    """
    return STYLED_404_BODY.replace(b"<p class=\"code\">404</p>",
                                     f'<p class="code">404</p><!--{seed}-->'.encode())


class MockHandler(http.server.BaseHTTPRequestHandler):
    """Routes documented probe paths and returns scenario-appropriate bodies."""

    # Set on the server instance, read here. Defaults to 'outage'.
    scenario: str = "outage"

    server_version = "Mock-WDGoWars/0.1"

    def log_message(self, fmt, *args):
        pass  # silent — integration tests are noisy enough

    def _send(self, status: int, body: bytes,
                content_type: str = "text/html; charset=utf-8",
                extra_headers: Optional[dict] = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _api_404(self):
        """Styled /api/* 404, optionally with per-path divergence."""
        if self.scenario == "diverged":
            self._send(404, _div_body(self.path), "text/html; charset=utf-8")
        else:
            self._send(404, STYLED_404_BODY, "text/html; charset=utf-8")

    def _non_api_404(self):
        """Bare Django-style 404 for non-/api/ paths."""
        self._send(404, BARE_404_BODY, "text/html; charset=utf-8")

    def _route(self):
        """Return (status, body, content_type) for the path/method/scenario."""
        path = self.path
        method = self.command
        scenario = self.scenario
        api_key = self.headers.get("X-API-Key", "")

        # Non-/api/ paths: changelog control + bare 404.
        if path == "/changelog":
            return 200, CHANGELOG_BODY, "text/html; charset=utf-8"
        if path == "/":
            return 200, b"<html>mock root</html>", "text/html; charset=utf-8"
        if not path.startswith("/api"):
            return 404, BARE_404_BODY, "text/html; charset=utf-8"

        # /api/* surface
        if scenario in ("outage", "diverged"):
            # Whole /api/* unbound — every path returns the styled 404
            # (or a per-path variant under `diverged`). Except /api/stats
            # which falls through to the LSWS admin layer in `outage`.
            if path == "/api/stats" and method == "GET":
                return 200, STATS_LEAK_BODY, "application/json"
            if scenario == "diverged":
                return 404, _div_body(path), "text/html; charset=utf-8"
            return 404, STYLED_404_BODY, "text/html; charset=utf-8"

        # `partial` — API is up but /api/stats is still leaking
        if scenario == "partial" and path == "/api/stats" and method == "GET":
            return 200, STATS_LEAK_BODY, "application/json"

        # `healthy` and `partial` — real /api/* handlers
        if path.rstrip("/") == "/api":
            return 200, b"<html>api root</html>", "text/html; charset=utf-8"
        if path.startswith("/api/me"):
            if method != "GET":
                return 405, b"Method Not Allowed", "text/plain"
            if not api_key:
                return 401, b'{"error":"missing X-API-Key"}', "application/json"
            if api_key == "g" * 64:
                return 401, b'{"error":"invalid key"}', "application/json"
            return 200, ME_OK_BODY, "application/json"
        if path.startswith("/api/upload-history"):
            if method != "GET":
                return 405, b"Method Not Allowed", "text/plain"
            if not api_key or api_key == "g" * 64:
                return 401, b'{"error":"auth required"}', "application/json"
            return 200, UPLOAD_HISTORY_OK_BODY, "application/json"
        if path == "/api/upload-csv":
            if method != "POST":
                return 405, b"Method Not Allowed", "text/plain"
            if not api_key or api_key == "g" * 64:
                return 401, b'{"error":"auth required"}', "application/json"
            return 200, UPLOAD_CSV_OK_BODY, "application/json"
        if path == "/api/upload/":
            # POST-only endpoint. GET should return 405 when healthy.
            if method == "GET":
                return 405, b"Method Not Allowed", "text/plain"
            if not api_key or api_key == "g" * 64:
                return 401, b'{"error":"auth required"}', "application/json"
            return 200, b'{"ok":true}', "application/json"
        if path == "/api/stats":
            # Healthy mode: explicitly blocked at the location level.
            return 404, b"Not Found", "text/plain"
        if path.startswith("/api/health"):
            return 404, b"Not Found", "text/plain"
        # Anything else under /api/ — bound but unknown route returns 404
        # with a distinct body (not the styled-404 fallback).
        return 404, b"Route not registered", "text/plain"

    def _handle(self):
        status, body, ct = self._route()
        self._send(status, body, ct)

    def do_GET(self):     self._handle()
    def do_POST(self):    self._handle()
    def do_PUT(self):     self._handle()
    def do_DELETE(self):  self._handle()
    def do_OPTIONS(self): self._handle()
    def do_HEAD(self):    self._handle()


def make_server(scenario: str = "outage",
                 port: int = 0) -> tuple[http.server.HTTPServer, int]:
    """Spawn the mock server bound to 127.0.0.1:`port`.

    port=0 picks a free port (returned via the second tuple element).
    """
    assert scenario in ("outage", "healthy", "partial", "diverged"), \
        f"unknown scenario: {scenario}"

    class _H(MockHandler):
        pass
    _H.scenario = scenario

    srv = http.server.HTTPServer(("127.0.0.1", port), _H)
    return srv, srv.server_address[1]


def serve_in_thread(scenario: str = "outage",
                     port: int = 0) -> tuple[http.server.HTTPServer, int]:
    """Same as make_server but starts serve_forever in a daemon thread.
    Returns (server, port). Call server.shutdown() to stop."""
    srv, port = make_server(scenario, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--scenario", default="outage",
                    choices=["outage", "healthy", "partial", "diverged"],
                    help="Which API state to simulate.")
    p.add_argument("--port", type=int, default=9999,
                    help="Port to bind (default 9999).")
    args = p.parse_args()
    srv, port = make_server(args.scenario, args.port)
    print(f"mock wdgwars on http://127.0.0.1:{port}  scenario={args.scenario}",
           file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
        srv.shutdown()


if __name__ == "__main__":
    main()
