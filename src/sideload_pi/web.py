"""Local web page for status, IPA upload, refresh, and the Apple verification code.

It listens on 127.0.0.1 unless `web.allow_remote` is set. Every request needs
the token from the root-only web.token file, sent as a cookie or as
`Authorization: Bearer`.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from sideload_pi.credentials import load_web_token
from sideload_pi.daemon import submit_code
from sideload_pi.errors import SideloadError
from sideload_pi.scheduling import utcnow

log = logging.getLogger("sideload_pi.web")

_NAME = re.compile(r'name="([^"]+)"')
_FILE = re.compile(r'filename="([^"]*)"')


def stream_multipart_file(read, content_type: str, dest: Path, limit: int, field: str = "ipa") -> str:
    """Write one multipart file part to ``dest`` without holding it in memory.

    ``read`` returns the next chunk, or b"" at EOF. The returned name is the
    filename from the part header. Anything larger than ``limit`` is refused
    and the partial file is removed.
    """
    match = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not match:
        raise SideloadError("upload was not a multipart form")
    boundary = match.group(1).encode("ascii", errors="ignore")
    marker = b"--" + boundary
    end_marker = b"\r\n" + marker
    buf = b""
    total = 0

    def pull() -> bool:
        nonlocal buf, total
        chunk = read(65536)
        if not chunk:
            return False
        total += len(chunk)
        if total > limit + 65536:
            raise SideloadError("upload is too large")
        buf += chunk
        return True

    try:
        while marker not in buf:
            if not pull():
                raise SideloadError("upload was not a multipart form")
        while True:
            start = buf.find(marker)
            if start < 0:
                if not pull():
                    raise SideloadError("missing ipa file")
                continue
            buf = buf[start + len(marker) :]
            if buf.startswith(b"--"):
                raise SideloadError("missing ipa file")
            while b"\r\n\r\n" not in buf:
                if not pull():
                    raise SideloadError("upload headers were truncated")
            header_blob, buf = buf.split(b"\r\n\r\n", 1)
            header_text = header_blob.decode("utf-8", errors="replace")
            name = ""
            filename = "upload.ipa"
            for line in header_text.splitlines():
                if not line.lower().startswith("content-disposition"):
                    continue
                found_name = re.search(r'name="([^"]*)"', line)
                found_file = re.search(r'filename="([^"]*)"', line)
                if found_name:
                    name = found_name.group(1)
                if found_file and found_file.group(1):
                    filename = Path(found_file.group(1)).name
            if name != field:
                while end_marker not in buf:
                    if not pull():
                        raise SideloadError("missing ipa file")
                    if len(buf) > len(end_marker) + 65536:
                        buf = buf[-(len(end_marker) + 8) :]
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with dest.open("wb") as handle:
                while True:
                    index = buf.find(end_marker)
                    if index >= 0:
                        piece = buf[:index]
                        written += len(piece)
                        if written > limit:
                            raise SideloadError("upload is too large")
                        handle.write(piece)
                        if not filename.lower().endswith(".ipa"):
                            raise SideloadError("file must end in .ipa")
                        return filename
                    keep = len(end_marker) - 1
                    if len(buf) > keep:
                        piece = buf[:-keep]
                        written += len(piece)
                        if written > limit:
                            raise SideloadError("upload is too large")
                        handle.write(piece)
                        buf = buf[-keep:]
                    if not pull():
                        raise SideloadError("upload ended before the file")
    except SideloadError:
        dest.unlink(missing_ok=True)
        raise


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    if "boundary=" not in content_type:
        raise SideloadError("upload was not a multipart form")
    boundary = content_type.split("boundary=", 1)[1].split(";", 1)[0].strip().strip('"')
    marker = b"--" + boundary.encode("utf-8")
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in body.split(marker):
        chunk = part.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        if chunk.startswith(b"--"):
            continue
        if b"\r\n\r\n" not in chunk:
            continue
        raw_head, data = chunk.split(b"\r\n\r\n", 1)
        if data.endswith(b"\r\n"):
            data = data[:-2]
        header = raw_head.decode("utf-8", errors="replace")
        name_match = _NAME.search(header)
        if not name_match:
            continue
        name = name_match.group(1)
        file_match = _FILE.search(header)
        if file_match and file_match.group(1):
            files[name] = (file_match.group(1), data)
        else:
            fields[name] = data.decode("utf-8", errors="replace").strip()
    return fields, files


def _token_ok(presented: str, expected: str) -> bool:
    if not expected or not presented:
        return False
    if len(presented) != len(expected):
        return False
    return hmac.compare_digest(presented, expected)


class WebApp:
    def __init__(self, daemon):
        self.daemon = daemon

    def expected_token(self) -> str:
        return load_web_token(self.daemon.layout)

    def handle(self, method: str, path: str, headers: dict, body: bytes, presented: str):
        token = self.expected_token()
        if path == "/login" and method == "POST":
            form = parse_qs(body.decode("utf-8", errors="replace"))
            given = (form.get("token") or [""])[0]
            if _token_ok(given, token):
                return 303, {"Set-Cookie": "sideload_token=" + given + "; HttpOnly; SameSite=Strict; Path=/", "Location": "/"}, b""
            return 401, {"Content-Type": "text/html; charset=utf-8"}, _page(login=True, error="That token did not match.")
        if not _token_ok(presented, token):
            return 401, {"Content-Type": "text/html; charset=utf-8"}, _page(login=True, error="")
        if method == "GET" and path == "/":
            return 200, {"Content-Type": "text/html; charset=utf-8"}, _page(status=self.daemon.status())
        if method == "GET" and path == "/api/status":
            payload = json.dumps(self.daemon.status()).encode("utf-8")
            return 200, {"Content-Type": "application/json"}, payload
        if method == "POST" and path == "/api/refresh":
            form = parse_qs(body.decode("utf-8", errors="replace"))
            bundle = (form.get("bundle_id") or [""])[0]
            self.daemon.request_refresh(bundle)
            return 303, {"Location": "/"}, b""
        if method == "POST" and path == "/api/2fa":
            form = parse_qs(body.decode("utf-8", errors="replace"))
            code = (form.get("code") or [""])[0]
            phone = (form.get("phone_id") or [""])[0]
            try:
                submit_code(self.daemon.layout, code, phone)
            except SideloadError as exc:
                return 400, {"Content-Type": "text/plain; charset=utf-8"}, str(exc).encode()
            return 303, {"Location": "/"}, b""
        if method == "POST" and path == "/api/remove":
            form = parse_qs(body.decode("utf-8", errors="replace"))
            bundle = (form.get("bundle_id") or [""])[0]
            try:
                self.daemon.library.remove(bundle)
            except SideloadError as exc:
                return 400, {"Content-Type": "text/plain; charset=utf-8"}, str(exc).encode()
            return 303, {"Location": "/"}, b""
        if method == "POST" and path == "/api/upload":
            return self._upload(headers, body)
        return 404, {"Content-Type": "text/plain"}, b"not found"

    def _upload(self, headers: dict, body: bytes):
        limit = self.daemon.config.web.max_upload_mb * 1024 * 1024
        if len(body) > limit:
            return 413, {"Content-Type": "text/plain"}, b"upload is too large"
        try:
            _fields, files = parse_multipart(headers.get("content-type", ""), body)
        except SideloadError as exc:
            return 400, {"Content-Type": "text/plain; charset=utf-8"}, str(exc).encode()
        if "ipa" not in files:
            return 400, {"Content-Type": "text/plain"}, b"missing ipa file"
        filename, data = files["ipa"]
        if not filename.lower().endswith(".ipa"):
            return 400, {"Content-Type": "text/plain"}, b"file must end in .ipa"
        dest = self.daemon.layout.work / "upload.ipa"
        self.daemon.layout.work.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return self.finish_upload(dest)

    def finish_upload(self, dest: Path):
        try:
            self.daemon.library.add_ipa(
                dest,
                max_apps=self.daemon.config.max_apps,
                strip_extensions=self.daemon.config.strip_extensions,
                now=utcnow(),
                window=timedelta(days=self.daemon.config.app_id_window_days),
                app_id_limit=self.daemon.config.max_app_ids_per_window,
            )
        except SideloadError as exc:
            return 400, {"Content-Type": "text/plain; charset=utf-8"}, str(exc).encode()
        self.daemon.request_refresh()
        return 303, {"Location": "/"}, b""


def _page(status: dict | None = None, login: bool = False, error: str = "") -> bytes:
    if login or status is None:
        body = f"""<!doctype html><meta charset="utf-8"><title>sideload-pi</title>
        <h1>sideload-pi</h1><p>{error}</p>
        <form method="post" action="/login"><label>Token <input name="token" type="password" required></label>
        <button type="submit">Unlock</button></form>"""
        return body.encode()
    rows = []
    for app in status["apps"]:
        days = "new" if app["days"] is None else f"{app['days']}d"
        rows.append(
            f"<tr><td>{_esc(app['name'])}</td><td>{_esc(app['bundle_id'])}</td>"
            f"<td>{days}</td><td>{_esc(app['last_error'])}</td></tr>"
        )
    table = "".join(rows) or "<tr><td colspan=4>No IPAs yet</td></tr>"
    device = status["device"]
    device_text = "not connected" if not device else f"{_esc(device['name'])} via {_esc(device['via'])}"
    percent = "" if status["percent"] is None else f" {status['percent']}%"
    body = f"""<!doctype html><meta charset="utf-8"><title>sideload-pi</title>
    <meta http-equiv="refresh" content="5">
    <h1>sideload-pi</h1>
    <p>Phase: {_esc(status['phase'])}{_esc(percent)} {_esc(status['message'])}</p>
    <p>Phone: {device_text}. Battery: {_esc(_battery(status))}.</p>
    <table border="1" cellpadding="4"><tr><th>App</th><th>Bundle</th><th>Left</th><th>Last error</th></tr>{table}</table>
    <h2>Add an IPA</h2>
    <form method="post" action="/api/upload" enctype="multipart/form-data">
    <input type="file" name="ipa" accept=".ipa" required><button type="submit">Upload and refresh</button></form>
    <h2>Refresh</h2>
    <form method="post" action="/api/refresh"><button type="submit">Refresh due apps</button></form>
    <h2>Apple verification code</h2>
    <p>{_phones(status)}</p>
    <form method="post" action="/api/2fa">
    <label>Phone id <input name="phone_id" inputmode="numeric"></label>
    <input name="code" inputmode="numeric" pattern="[0-9]{{6}}" required>
    <button type="submit">Submit code</button></form>
    """
    return body.encode()


def _phones(status: dict) -> str:
    phones = status.get("sms_phones") or []
    if not phones:
        return "Leave the phone id empty unless the panel lists more than one number."
    bits = [f"{_esc(item.get('id'))} ({_esc(item.get('label'))})" for item in phones]
    return "SMS numbers: " + ", ".join(bits) + ". Send the id with the code."


def _battery(status: dict) -> str:
    percent = status.get("battery_percent")
    if percent is None:
        return "not read"
    charging = status.get("charging")
    suffix = " charging" if charging is True else (" on battery" if charging is False else "")
    return f"{int(round(percent))}%{suffix}"


def _esc(value) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def serve_in_thread(daemon) -> threading.Thread | None:
    token = load_web_token(daemon.layout)
    if not token:
        log.warning("web UI is off because %s does not exist yet", daemon.layout.web_token)
        return None
    app = WebApp(daemon)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self._respond()

        def do_POST(self):  # noqa: N802
            self._respond()

        def _presented(self) -> str:
            presented = ""
            auth = self.headers.get("Authorization") or ""
            if auth.startswith("Bearer "):
                presented = auth[len("Bearer ") :]
            cookie = self.headers.get("Cookie") or ""
            for piece in cookie.split(";"):
                piece = piece.strip()
                if piece.startswith("sideload_token="):
                    presented = piece.split("=", 1)[1]
            return presented

        def _respond(self) -> None:
            path = self.path.split("?", 1)[0]
            if self.command == "POST" and path == "/api/upload":
                status, headers, payload = self._streamed_upload(app)
            else:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 1024 * 1024:
                    self.send_response(413)
                    self.end_headers()
                    return
                body = b""
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    body += chunk
                    remaining -= len(chunk)
                status, headers, payload = app.handle(
                    self.command,
                    path,
                    {key.lower(): value for key, value in self.headers.items()},
                    body,
                    self._presented(),
                )
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(payload)

        def _streamed_upload(self, app: WebApp):
            if not _token_ok(self._presented(), app.expected_token()):
                return 401, {"Content-Type": "text/html; charset=utf-8"}, _page(login=True, error="")
            length = int(self.headers.get("Content-Length") or 0)
            limit = app.daemon.config.web.max_upload_mb * 1024 * 1024
            if length > limit + 65536:
                return 413, {"Content-Type": "text/plain"}, b"upload is too large"
            dest = app.daemon.layout.work / "upload.ipa"
            remaining = length

            def read(size: int) -> bytes:
                nonlocal remaining
                if remaining <= 0:
                    return b""
                chunk = self.rfile.read(min(size, remaining))
                remaining -= len(chunk)
                return chunk

            try:
                stream_multipart_file(
                    read,
                    self.headers.get("Content-Type") or "",
                    dest,
                    limit,
                )
            except SideloadError as exc:
                code = 413 if "too large" in str(exc) else 400
                return code, {"Content-Type": "text/plain; charset=utf-8"}, str(exc).encode()
            return app.finish_upload(dest)

        def log_message(self, fmt, *args):
            log.info("web " + fmt, *args)

    server = ThreadingHTTPServer((daemon.config.web.bind, daemon.config.web.port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="sideload-pi-web", daemon=True)
    thread.start()
    log.info("web UI on http://%s:%s/", daemon.config.web.bind, daemon.config.web.port)
    return thread
