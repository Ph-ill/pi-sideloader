import pytest
from tests.support import make_ipa

from sideload_pi.config import Config
from sideload_pi.credentials import save_web_token
from sideload_pi.layout import Layout
from sideload_pi.library import Library
from sideload_pi.web import WebApp, parse_multipart


class _Stub:
    def __init__(self, layout, library, config):
        self.layout = layout
        self.library = library
        self.config = config
        self.refreshed = []

    def request_refresh(self, bundle_id=""):
        self.refreshed.append(bundle_id)

    def status(self):
        return {
            "phase": "idle",
            "message": "",
            "detail": "",
            "percent": None,
            "active_name": "",
            "active_bundle": "",
            "device": None,
            "battery_percent": 50,
            "charging": False,
            "apps": [],
        }


def test_multipart_and_token_cookie(tmp_path):
    ipa = make_ipa(tmp_path / "notes.ipa")
    body = (
        b'--BOUND\r\nContent-Disposition: form-data; name="ipa"; filename="notes.ipa"\r\n\r\n'
        + ipa.read_bytes()
        + b"\r\n--BOUND--\r\n"
    )
    _fields, files = parse_multipart("multipart/form-data; boundary=BOUND", body)
    assert files["ipa"][0] == "notes.ipa"
    layout = Layout(tmp_path / "data")
    layout.ensure()
    library = Library(layout)
    config = Config(root=layout.root)
    save_web_token(layout, "tokentokentoken")
    app = WebApp(_Stub(layout, library, config))
    status, _headers, payload = app.handle("GET", "/", {}, b"", "")
    assert status == 401
    assert b"Unlock" in payload
    status, headers, _payload = app.handle(
        "POST",
        "/login",
        {},
        b"token=tokentokentoken",
        "",
    )
    assert status == 303
    assert "sideload_token=tokentokentoken" in headers["Set-Cookie"]
    status, _headers, payload = app.handle(
        "POST",
        "/api/upload",
        {"content-type": "multipart/form-data; boundary=BOUND"},
        body,
        "tokentokentoken",
    )
    assert status == 303
    assert "com.example.notes" in library.apps
    status, _headers, _payload = app.handle("POST", "/api/2fa", {}, b"code=123456", "tokentokentoken")
    assert status == 303
    from sideload_pi.daemon import pop_code

    assert pop_code(layout) == "123456"
    assert app.daemon.refreshed == [""]


def test_upload_streams_to_disk_in_chunks(tmp_path):
    from sideload_pi.web import stream_multipart_file

    ipa = make_ipa(tmp_path / "notes.ipa")
    raw = ipa.read_bytes()
    body = (
        b'--BOUND\r\nContent-Disposition: form-data; name="ipa"; filename="notes.ipa"\r\n\r\n'
        + raw
        + b"\r\n--BOUND--\r\n"
    )
    offset = {"n": 0}

    def read(size):
        start = offset["n"]
        chunk = body[start : start + 7]
        offset["n"] = start + len(chunk)
        return chunk

    dest = tmp_path / "out.ipa"
    name = stream_multipart_file(read, "multipart/form-data; boundary=BOUND", dest, limit=len(raw))
    assert name == "notes.ipa"
    assert dest.read_bytes() == raw
    offset["n"] = 0
    with pytest.raises(Exception, match="too large"):
        stream_multipart_file(
            read, "multipart/form-data; boundary=BOUND", tmp_path / "big.ipa", limit=8
        )
    assert not (tmp_path / "big.ipa").exists()
