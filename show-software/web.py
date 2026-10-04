"""
Local web server (standard library only).

    /                 operator panel (open on your phone over local Wi-Fi)
    /mirror           the mirror page (Chromium kiosk on the hidden TV)
    /mirror/<file>    figure video, fonts, calibration page
    /events           live command stream for the mirror (Server-Sent Events)
    /api/status       JSON state for the panel
    /api/cmd          POST {"action": "..."} from the panel
"""
from __future__ import annotations

import json
import logging
import mimetypes
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("web")
ACTIONS = {"start", "reset", "pause", "resume", "mute", "unmute", "ring_test",
           "text_test", "figure_test", "black", "clear_alert"}


class MirrorChannel:
    """What the show calls to drive the mirror. Broadcasts to every open mirror page."""

    def __init__(self, echo: bool = False):
        self.echo = echo
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def show_text(self, text: str, fade_in: float, hold: float, fade_out: float) -> None:
        self._send({"cmd": "text", "text": text, "fade_in": fade_in, "hold": hold, "fade_out": fade_out},
                   f"🪞 MIRROR: the words rise out of the dark — “{text}”")

    def figure_in(self, fade_s: float) -> None:
        self._send({"cmd": "figure_in", "fade": fade_s},
                   f"🪞 MIRROR: a figure begins to fade in behind them ({fade_s:.0f}s)…")

    def cut(self) -> None:
        self._send({"cmd": "cut"}, "🪞 MIRROR: cut to black")

    def black(self) -> None:
        self._send({"cmd": "black"}, None)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def clients(self) -> int:
        return len(self._subs)

    def _send(self, msg: dict, echo: str | None) -> None:
        if self.echo and echo:
            print(f"  {echo}", flush=True)
        data = json.dumps(msg)
        with self._lock:
            for q in self._subs:
                q.put(data)


class WebServer:
    def __init__(self, cfg, base: Path, mirror: MirrorChannel, show, loop):
        self.host, self.port = cfg["web"]["host"], cfg["web"]["port"]
        self.token = cfg["web"].get("operator_token") or ""
        self.base, self.mirror, self.show, self.loop = base, mirror, show, loop

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                url = urlparse(self.path)
                if url.path == "/events":
                    return self._events()
                if url.path in ("/mirror", "/mirror/"):
                    return self._file(server.base / "mirror" / "index.html")
                if url.path.startswith("/mirror/"):
                    root = (server.base / "mirror").resolve()
                    target = (root / url.path[len("/mirror/"):]).resolve()
                    if root in target.parents and target.is_file():
                        return self._file(target)
                    return self._send(404, b"not found", "text/plain")
                if not self._authorized(url):
                    return self._send(403, b"add ?token=... to the URL", "text/plain")
                if url.path == "/":
                    return self._file(server.base / "operator.html")
                if url.path == "/api/status":
                    status = dict(server.show.status(), mirror_pages=server.mirror.clients())
                    return self._send(200, json.dumps(status).encode(), "application/json")
                self._send(404, b"not found", "text/plain")

            def do_POST(self):
                url = urlparse(self.path)
                if url.path != "/api/cmd" or not self._authorized(url):
                    return self._send(403, b"forbidden", "text/plain")
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    action = json.loads(self.rfile.read(length) or b"{}").get("action", "")
                except ValueError:
                    return self._send(400, b"bad json", "text/plain")
                if action not in ACTIONS:
                    return self._send(400, b"unknown action", "text/plain")
                server.loop.call_soon_threadsafe(server.show.operator, action)
                self._send(200, b'{"ok":true}', "application/json")

            # ── helpers ──
            def _authorized(self, url) -> bool:
                if not server.token:
                    return True
                return (parse_qs(url.query).get("token", [""])[0] == server.token
                        or self.headers.get("X-Token") == server.token)

            def _send(self, code, body: bytes, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _file(self, path: Path):
                if not path.is_file():
                    return self._send(404, b"not found", "text/plain")
                ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self._send(200, path.read_bytes(), ctype)

            def _events(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                q = server.mirror.subscribe()
                log.info("mirror page connected (%d open)", server.mirror.clients())
                try:
                    self.wfile.write(b"retry: 1000\n\n")
                    self.wfile.write(b'data: {"cmd":"black"}\n\n')
                    self.wfile.flush()
                    while True:
                        try:
                            msg = q.get(timeout=15)
                            self.wfile.write(f"data: {msg}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    server.mirror.unsubscribe(q)
                    log.info("mirror page disconnected")

        httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True, name="web").start()
        log.info("operator panel: http://%s:%d/   mirror: http://localhost:%d/mirror",
                 "localhost" if self.host in ("0.0.0.0", "") else self.host, self.port, self.port)
