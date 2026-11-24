"""Local SRP peer plus SPD and app-token fixtures. This does not call Apple."""

import hashlib
import hmac
import os
import plistlib

import srp._pysrp as srp
from srp._pysrp import _hash_map, gen_x, get_ng, long_to_bytes

from sideload_pi.anisette import AnisetteData
from sideload_pi.errors import AppleAuthError
from sideload_pi.gsa import (
    XCODE_APP,
    GsaClient,
    decrypt_app_token,
    decrypt_spd,
    derive_password,
    encrypt_app_token,
    encrypt_spd,
    negotiation_digest,
    session_hmac,
)
from sideload_pi.http_client import HttpResponse

srp.rfc5054_enable()
srp.no_username_in_x()


def _anisette():
    return AnisetteData(
        md="md",
        md_m="mdm",
        md_lu="lu",
        rinfo="17106176",
        device_id="device",
        serial="0",
        client_info="<PC> <Windows;6.2(0,0);9200> <com.apple.AuthKit/1 (com.apple.iTunes/12.12.4.1)>",
        locale="en_US",
        timezone="UTC",
        client_time="2026-01-01T00:00:00Z",
        user_agent="akd/1.0 CFNetwork/808.1.4",
    )


def _verifier(apple_id, password, salt):
    derived = derive_password(password, salt, 2000, "s2k")
    modulus, generator = get_ng(srp.NG_2048, None, None)
    exponent = gen_x(_hash_map[srp.SHA256], salt, apple_id, derived)
    public = long_to_bytes(pow(generator, exponent, modulus))
    return derived, public


class _Server:
    def __init__(self, password="correct-horse", mismatch=False):
        self.password = password
        self.mismatch = mismatch
        self.apple_id = "person@example.com"
        self.salt = os.urandom(16)
        _derived, self.verifier_bytes = _verifier(self.apple_id, password, self.salt)
        self.verifier = None
        self.sk = os.urandom(32)
        self.cookie = os.urandom(8)
        self.seen_headers = []
        self.seen_cpd = []

    def request(self, method, url, body=None, headers=None, timeout=None, follow_redirects=True):
        self.seen_headers.append(dict(headers or {}))
        req = plistlib.loads(body)["Request"]
        self.seen_cpd.append(req.get("cpd"))
        op = req["o"]
        if op == "init":
            self.verifier = srp.Verifier(
                self.apple_id,
                self.salt,
                self.verifier_bytes,
                req["A2k"],
                hash_alg=srp.SHA256,
                ng_type=srp.NG_2048,
            )
            salt, public_b = self.verifier.get_challenge()
            inner = {
                "Status": {"ec": 0},
                "sp": "s2k",
                "s": salt,
                "i": 2000,
                "B": public_b,
                "c": b"cookie",
            }
        elif op == "complete":
            proof = self.verifier.verify_session(req["M1"])
            if not self.verifier.authenticated() or not isinstance(proof, bytes):
                proof = b"\x00" * 32
            if self.mismatch:
                proof = bytes(byte ^ 0xFF for byte in proof)
            key = self.verifier.get_session_key() or b"\x11" * 32
            spd_plain = plistlib.dumps(
                {
                    "adsid": "ads-1",
                    "GsIdmsToken": "idms-token",
                    "sk": self.sk,
                    "c": self.cookie,
                }
            )
            spd = encrypt_spd(key, spd_plain)
            digest = negotiation_digest("s2k", spd, None)
            np_value = hmac.new(session_hmac(key, "HMAC key:"), digest, hashlib.sha256).digest()
            if self.mismatch:
                np_value = bytes(byte ^ 0xFF for byte in np_value)
            inner = {"Status": {"ec": 0}, "M2": proof, "spd": spd, "np": np_value}
        else:
            token = plistlib.dumps(
                {"t": {XCODE_APP: {"token": "xcode-token", "expiry": 1_700_000_000_000}}}
            )
            inner = {"Status": {"ec": 0}, "et": encrypt_app_token(self.sk, b"0123456789abcdef", token)}
        return HttpResponse(200, plistlib.dumps({"Response": inner}), {})


# Frozen outputs of the published algorithms (PBKDF2-HMAC-SHA256, AES-256-CBC
# PKCS7, the AltSign negotiation digest, AES-GCM "XYZ"). Computed with hashlib
# and cryptography, not by calling the functions under test.
_SALT = bytes.fromhex("00112233445566778899aabbccddeeff")
_S2K = bytes.fromhex("73a47d5f0a7d86c8e4c9e6aa266d65c45c823112f1aacda5ecd99aa39d60f401")
_S2K_FO = bytes.fromhex("c8d36964d09b5a4b78323bd448f7fb35dd2d94bd11ad23760f03a49e12e5a5bc")
_SPD = bytes.fromhex("a7ca08d68d435ad8546923e25521fddc")
_NP = bytes.fromhex("c8c53cccaaa89d5ca04564f801e113b0a7cff6003c530e53b8aed39c765f8c15")
_GCM = bytes.fromhex(
    "58595a303132333435363738396162636465666281b554fdc5d4a13f32a655e86e30a969752fbc69af2b938bdf99"
)
_CHECKSUM = bytes.fromhex("58f2f921b4c2b76f643f00ab5295cf59642e76a7a42903de6ec0246bf309a99b")


def test_password_derivation_matches_the_published_vectors():
    assert derive_password("correct-horse", _SALT, 1000, "s2k") == _S2K
    assert derive_password("correct-horse", _SALT, 1000, "s2k_fo") == _S2K_FO
    assert _S2K != _S2K_FO


def test_spd_negotiation_and_app_token_match_frozen_vectors():
    session = bytes(range(32))
    assert encrypt_spd(session, b"hello-spd") == _SPD
    assert decrypt_spd(session, _SPD) == b"hello-spd"
    digest = negotiation_digest("s2k", b"abcd", b"ef")
    mac = hmac.new(session_hmac(session, "HMAC key:"), digest, hashlib.sha256).digest()
    assert mac == _NP
    sk = bytes([0x22] * 32)
    assert decrypt_app_token(sk, _GCM) == b"token-plist"
    from sideload_pi.gsa import XCODE_APP, app_token_checksum

    assert app_token_checksum(sk, "ads-1", [XCODE_APP]) == _CHECKSUM


def test_spd_and_app_token_round_trip():
    key = os.urandom(32)
    blob = encrypt_spd(key, b"hello-spd-payload!!")
    assert decrypt_spd(key, blob) == b"hello-spd-payload!!"
    iv = os.urandom(16)
    wrapped = encrypt_app_token(key, iv, b"token-plist")
    assert decrypt_app_token(key, wrapped) == b"token-plist"


def test_s2k_and_s2k_fo_are_different_passwords():
    salt = b"salt-bytes-16b!"
    assert derive_password("secret", salt, 10, "s2k") != derive_password("secret", salt, 10, "s2k_fo")


def test_login_against_a_local_srp_peer():
    server = _Server()
    client = GsaClient(server)
    data = _anisette()
    session = client.authenticate("Person@Example.com", "correct-horse", lambda: data, lambda kind: "000000")
    assert session.adsid == "ads-1"
    assert session.gs_token == "xcode-token"
    assert session.token_expiry is not None
    assert server.seen_headers[0]["User-Agent"] == data.user_agent
    assert server.seen_headers[0]["X-MMe-Client-Info"] == data.client_info
    assert server.seen_cpd[0]["bootstrap"] == "true"
    assert server.seen_cpd[0]["pbe"] == "false"
    assert "X-Apple-I-MD-RINFO" not in server.seen_cpd[0]
    assert server.apple_id == "person@example.com"


def test_hsc_434_resyncs_once_and_reuses_the_same_A2k():
    server = _Server()
    attempts = {"init": 0}
    resyncs = []
    original = server.request

    def request(method, url, body=None, headers=None, timeout=None, follow_redirects=True):
        if url.endswith("/v3/resync"):
            resyncs.append(body)
            return HttpResponse(200, b"{}", {})
        payload = plistlib.loads(body)["Request"]
        if payload["o"] == "init":
            attempts["init"] += 1
            if attempts["init"] == 1:
                attempts["A2k"] = payload["A2k"]
                inner = {
                    "Status": {"ec": -22421, "hsc": 434, "X-Apple-I-MD-DATA": "sim-data"}
                }
                return HttpResponse(200, plistlib.dumps({"Response": inner}), {})
            assert payload["A2k"] == attempts["A2k"]
        return original(method, url, body, headers, timeout, follow_redirects)

    server.request = request
    client = GsaClient(server)

    def resync(sim):
        server.request("POST", "http://anisette/v3/resync", body=sim.encode(), headers={})

    session = client.authenticate(
        "person@example.com",
        "correct-horse",
        _anisette,
        lambda kind: "000000",
        anisette_resync=resync,
    )
    assert session.gs_token == "xcode-token"
    assert attempts["init"] == 2
    assert resyncs == [b"sim-data"]


def test_bad_password_does_not_produce_a_session():
    server = _Server(mismatch=True)
    client = GsaClient(server)
    with pytest_raises():
        client.authenticate("person@example.com", "correct-horse", _anisette, lambda kind: "000000")


def test_sms_uses_the_listed_phone_id():
    import json

    class Http:
        def __init__(self):
            self.bodies = []

        def request(self, method, url, body=None, headers=None, timeout=None, follow_redirects=True):
            self.bodies.append((method, url, body))
            if url.rstrip("/").endswith("/auth"):
                payload = {
                    "trustedPhoneNumbers": [
                        {"id": 4, "lastTwoDigits": "19", "pushMode": "sms", "numberWithDialCode": "+1 •••19"},
                        {"id": 7, "lastTwoDigits": "42", "pushMode": "sms"},
                    ]
                }
                return HttpResponse(200, json.dumps(payload).encode(), {})
            return HttpResponse(200, b"{}", {})

    http = Http()
    GsaClient(http)._sms_code({}, lambda kind, phones: "123456" if len(phones) == 1 else "7:123456")
    sent = next(body for method, url, body in http.bodies if method == "PUT")
    assert b'"id": 7' in sent
    assert b'"id": 1' not in sent


def pytest_raises():
    import pytest

    return pytest.raises(AppleAuthError)
