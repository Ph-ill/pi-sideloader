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
        client_info="<iMac11,3>",
        locale="en_US",
        timezone="UTC",
        client_time="2026-01-01T00:00:00Z",
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

    def request(self, method, url, body=None, headers=None, timeout=None):
        req = plistlib.loads(body)["Request"]
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
    session = client.authenticate("person@example.com", "correct-horse", _anisette, lambda kind: "000000")
    assert session.adsid == "ads-1"
    assert session.gs_token == "xcode-token"
    assert session.token_expiry is not None


def test_bad_password_does_not_produce_a_session():
    server = _Server(mismatch=True)
    client = GsaClient(server)
    with pytest_raises():
        client.authenticate("person@example.com", "correct-horse", _anisette, lambda kind: "000000")


def pytest_raises():
    import pytest

    return pytest.raises(AppleAuthError)
