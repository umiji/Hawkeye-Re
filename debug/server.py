"""Local inspection server for Hawkeye's market data.

    .venv/Scripts/python.exe debug/server.py        # -> http://127.0.0.1:8765

Why a server and not a plain HTML file: the Finnhub key must never reach
the browser, and Finnhub rejects browser-origin requests anyway. The page
asks this process; this process asks Finnhub. It binds the loopback
interface only, so nothing on the network can reach it.

`index.html` is read from disk on every request, so editing the page needs
no restart.
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hawkeye.envfile import load_local_env          # noqa: E402
from debug.probe import ProbeError, probe_ticker    # noqa: E402

PAGE = Path(__file__).resolve().parent / "index.html"


class Handler(BaseHTTPRequestHandler):
    server_version = "HawkeyeDebug/1.0"

    def do_GET(self) -> None:                        # noqa: N802 — stdlib API
        route = urlparse(self.path)
        if route.path in ("/", "/index.html"):
            self._send_page()
        elif route.path == "/api/probe":
            self._send_probe(parse_qs(route.query))
        else:
            self._send_json(404, {"error": f"no route {route.path}"})

    def _send_page(self) -> None:
        try:
            body = PAGE.read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": f"index.html: {exc}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_probe(self, query: dict[str, list[str]]) -> None:
        ticker = (query.get("ticker") or [""])[0]
        try:
            payload = probe_ticker(ticker)
        except ProbeError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:                     # noqa: BLE001
            # An unexpected failure is the thing this tool exists to show,
            # so it is reported to the page rather than only to the console.
            self._send_json(502, {"error": f"{type(exc).__name__}: {exc}"})
        else:
            self._send_json(200, payload)

    def _send_json(self, status: int, payload: dict) -> None:
        # allow_nan=False so a future NaN leak raises here instead of shipping
        # bare NaN, which is not valid JSON: the browser then fails to parse
        # the whole response and the page goes blank with nothing in the
        # server log to explain it (what happened on 2026-08-02). A visible
        # server-side error beats a silent blank page.
        body = json.dumps(payload, ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1",
                        help="loopback by default; change only if you accept"
                             " exposing the API key's results to the network")
    parser.add_argument("--no-open", action="store_true",
                        help="do not open a browser window")
    args = parser.parse_args(argv)

    # The key lives in .env.local at the repo root, not the shell.
    load_local_env(REPO_ROOT)

    url = f"http://{args.host}:{args.port}/"
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(f"Hawkeye debug UI: {url}  (Ctrl+C to stop)\n")
    if not args.no_open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nstopped\n")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
