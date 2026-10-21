"""Local HTTP server: serves the UI and a JSON API on 127.0.0.1 only.

Every API request needs the session token (X-Token header, or ?t= for GET
downloads/previews) and a 127.0.0.1/localhost Host header.

Feature modules add endpoints with the @route decorator:

    from retrova.server import route, Reply

    @route("GET", "/api/thing")
    def thing(q, body):            # q: dict of query params (single values), body: parsed JSON (POST)
        return {"ok": True}        # dict/list -> JSON 200; raise ApiError(msg, code) for errors;
                                   # return Reply(...) for files/streams.
"""
from __future__ import annotations

import hmac
import json
import mimetypes
import os
import secrets
import socketserver
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .jobs import JOBS

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
ROUTES: dict[tuple[str, str], callable] = {}
PREFIX_ROUTES: list[tuple[str, str, callable]] = []


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


class Reply:
    """A raw response: bytes, or a file-like object streamed in chunks."""

    def __init__(self, body=b"", ctype="application/octet-stream", code=200, headers=None, stream=None, length=None):
        self.body, self.ctype, self.code = body, ctype, code
        self.headers = headers or {}
        self.stream, self.length = stream, length


def route(method: str, path: str, prefix: bool = False):
    def deco(fn):
        if prefix:
            PREFIX_ROUTES.append((method, path, fn))
        else:
            ROUTES[(method, path)] = fn
        return fn
    return deco


# ---------------------------------------------------------------- built-in endpoints
@route("GET", "/api/jobs")
def _jobs(q, body):
    return {"jobs": JOBS.list(q.get("kind"))}


@route("GET", "/api/job")
def _job(q, body):
    return JOBS.get(q.get("id", "")).status()


@route("GET", "/api/job/result")
def _job_result(q, body):
    return JOBS.get(q.get("id", "")).result(q)


@route("POST", "/api/job/control")
def _job_control(q, body):
    j = JOBS.get(str(body.get("id", "")))
    action = body.get("action")
    if action not in ("pause", "resume", "stop"):
        raise ApiError("action must be pause, resume or stop")
    getattr(j, action)()
    return j.status()


class _LocalHTTPServer(ThreadingHTTPServer):
    """HTTPServer.server_bind() looks up the host's name with socket.getfqdn(). On macOS that
    reverse lookup can go to multicast DNS and stall the first launch for minutes (or wait on the
    Local Network privacy check). The engine only ever listens on 127.0.0.1, so skip the lookup."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class Server:
    def __init__(self, port=0, token=None):
        self.token = token or secrets.token_urlsafe(24)
        self.port = port
        handler = make_handler(self)
        last = None
        for p in ([port] if port else [0]):
            try:
                self.httpd = _LocalHTTPServer(("127.0.0.1", p), handler)
                break
            except OSError as ex:
                last = ex
        else:
            raise last
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def serve_forever(self):
        self.httpd.serve_forever()

    def shutdown(self):
        self.httpd.shutdown()


def make_handler(server: Server):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Retrova/" + __version__
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _host_ok(self):
            return self.headers.get("Host", "") in (f"127.0.0.1:{server.port}", f"localhost:{server.port}")

        def _auth(self, qs):
            tok = self.headers.get("X-Token") or qs.get("t", [""])[0]
            return hmac.compare_digest(tok.encode(), server.token.encode())

        def _send(self, code, body: bytes, ctype, headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def _static(self, path):
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            full = os.path.normpath(os.path.join(UI_DIR, rel))
            if not full.startswith(UI_DIR) or not os.path.isfile(full):
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if full.endswith(".js") or full.endswith(".mjs"):
                ctype = "text/javascript"
            with open(full, "rb") as fh:
                data = fh.read()
            headers = {}
            if rel == "index.html":
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
                    "style-src 'self' 'unsafe-inline'; script-src 'self'; frame-src 'self'; connect-src 'self'")
            self._send(200, data, ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""), headers)

        def _dispatch(self, method):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if not self._host_ok():
                return self._json({"error": "bad host"}, 403)
            if method == "GET" and not u.path.startswith("/api/"):
                return self._static(u.path)
            if not self._auth(qs) or (method == "POST" and not self.headers.get("X-Token")):
                return self._json({"error": "unauthorised"}, 401)
            fn = ROUTES.get((method, u.path))
            if fn is None:
                for m, pre, f in PREFIX_ROUTES:
                    if m == method and u.path.startswith(pre):
                        fn = f
                        break
            if fn is None:
                return self._json({"error": "not found"}, 404)
            q = {k: v[0] for k, v in qs.items()}
            q["_path"] = u.path
            q["_range"] = self.headers.get("Range")
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                except ValueError:
                    return self._json({"error": "bad JSON"}, 400)
            try:
                out = fn(q, body)
            except ApiError as ex:
                return self._json({"error": str(ex)}, ex.code)
            except (KeyError, FileNotFoundError) as ex:
                return self._json({"error": str(ex).strip("'\"")}, 404)
            except (ValueError, PermissionError, RuntimeError) as ex:
                return self._json({"error": str(ex)}, 400)
            except Exception as ex:
                traceback.print_exc()
                return self._json({"error": f"{type(ex).__name__}: {ex}"}, 500)
            if isinstance(out, Reply):
                return self._reply(out)
            return self._json(out)

        def _reply(self, r: Reply):
            if r.stream is None:
                return self._send(r.code, r.body, r.ctype, r.headers)
            self.send_response(r.code)
            self.send_header("Content-Type", r.ctype)
            self.send_header("X-Content-Type-Options", "nosniff")
            if r.length is not None:
                self.send_header("Content-Length", str(r.length))
            for k, v in r.headers.items():
                self.send_header(k, v)
            self.end_headers()
            left = r.length
            try:
                while left is None or left > 0:
                    chunk = r.stream.read(1024 * 1024 if left is None else min(1024 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    if left is not None:
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                close = getattr(r.stream, "close", None)
                if close:
                    try:
                        close()
                    except Exception:
                        pass

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def guess_type(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


for ext, mt in ((".heic", "image/heic"), (".heif", "image/heif"), (".webp", "image/webp"),
                (".mov", "video/quicktime"), (".stl", "model/stl"), (".mjs", "text/javascript")):
    mimetypes.add_type(mt, ext)
