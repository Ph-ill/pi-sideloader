"""The public Mac anisette server's response shape, and HTTPS verification."""

import json
import ssl
import threading
import urllib.error
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from sideload_pi.anisette import AnisetteClient
from sideload_pi.http_client import HttpResponse, UrllibHttp

MAC_CLIENT_INFO = (
    "<MacBookPro13,3> <Mac OS X;10.12.6;16G29> "
    "<com.apple.AuthKit/1 (com.apple.akd/1.0)>"
)
MAC_USER_AGENT = "akd/1.0 CFNetwork/811.5.4 Darwin/16.7.0"


class _Http:
    def __init__(self, handler):
        self.handler = handler

    def request(self, method, url, body=None, headers=None, timeout=None, follow_redirects=True):
        return self.handler(method, url, body or b"", headers or {})


def test_mac_client_info_is_forwarded_unchanged():
    """GET /v3/client_info wins over the one-time-password body's client string."""

    def handler(method, url, body, headers):
        assert method == "GET"
        if str(url).endswith("/v3/client_info"):
            return HttpResponse(
                200,
                json.dumps(
                    {"client_info": MAC_CLIENT_INFO, "user_agent": MAC_USER_AGENT}
                ).encode(),
                {"implementation-version": "anisette-v3-server (native-c 0.2.0)"},
            )
        return HttpResponse(
            200,
            json.dumps(
                {
                    "X-Apple-I-MD": "otp",
                    "X-Apple-I-MD-M": "machine",
                    "X-Apple-I-MD-LU": "local-user",
                    "X-Apple-I-MD-RINFO": "33883392",
                    "X-Apple-I-SRL-NO": "0",
                    "X-Apple-I-TimeZone": "GMT",
                    "X-Apple-Locale": "en_US",
                    "X-Apple-I-Client-Time": "2026-11-24T18:00:00Z",
                    "X-Mme-Device-Id": "device-id",
                    "X-MMe-Client-Info": "<PC> <Windows;6.2(0,0);9200>",
                }
            ).encode(),
            {},
        )

    data = AnisetteClient("https://mac-ani.jkcoxson.com", http=_Http(handler)).fetch()
    assert data.client_info == MAC_CLIENT_INFO
    assert data.user_agent == MAC_USER_AGENT
    assert data.http_headers()["X-MMe-Client-Info"] == MAC_CLIENT_INFO
    assert data.rinfo == "33883392"
    assert data.md == "otp"
    assert data.client_info.startswith("<MacBookPro13,3>")
    assert "Darwin" in data.user_agent


def _self_signed_cert(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(IPv4Address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def test_https_client_rejects_a_certificate_the_system_does_not_trust(tmp_path):
    cert_path, key_path = _self_signed_cert(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"client_info":"x","user_agent":"y"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.URLError) as caught:
            UrllibHttp().request("GET", f"https://127.0.0.1:{port}/v3/client_info", timeout=5)
    finally:
        server.shutdown()
        server.server_close()
    assert "certificate" in str(caught.value).lower()
