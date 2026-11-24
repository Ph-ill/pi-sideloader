"""GrandSlam (GSA) Apple ID login, including a trusted-device verification code.

The request shape, password derivation, SPD decryption, negotiation proof, and
app-token exchange follow AltSign (rileytestut/AltSign, ALTAppleAPI+Authentication.m)
and the pure-Python client in nythepegasus/grandslam:

- SRP via the ``srp`` package's pure-Python module with ``rfc5054_enable`` and
  ``no_username_in_x``, which is the combination that avoids Apple's -22406.
- ``s2k`` feeds PBKDF2 the raw SHA-256 of the password. ``s2k_fo`` feeds it the
  hex encoding of that digest (AltSign's ALTPBKDF2SRP).
- SPD is AES-256-CBC with HMAC-SHA256(session, "extra data key:") and the first
  16 bytes of HMAC-SHA256(session, "extra data iv:").
- The Xcode token is not GsIdmsToken. After login, an ``apptokens`` request for
  ``com.apple.gs.xcode.auth`` returns ``et``, decrypted with AES-GCM. The blob
  is the ASCII ``XYZ``, a 16-byte IV, ciphertext, and a 16-byte tag. The AAD
  is those three version bytes. The checksum is HMAC-SHA256(sk, "apptokens" +
  adsid + app id).
- If status ``au`` is ``trustedDeviceSecondaryAuth`` or ``secondaryAuth``, the
  code is submitted and the whole login runs once more, which is what AltSign
  does after a successful code.

This was checked against a local SRP peer and fixture plists. It was not run
against Apple's servers in this repo's tests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import plistlib
import struct
from dataclasses import dataclass
from datetime import UTC, datetime

import srp._pysrp as srp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sideload_pi.anisette import AnisetteData
from sideload_pi.errors import AppleAuthError
from sideload_pi.http_client import UrllibHttp

srp.rfc5054_enable()
srp.no_username_in_x()

GSA_URL = "https://gsa.apple.com/grandslam/GsService2"
XCODE_APP = "com.apple.gs.xcode.auth"

_ERROR_TEXT = {
    -20101: "Apple ID or password was not accepted",
    -22406: "Apple ID or password was not accepted",
    -22421: "Apple rejected the anisette data",
    -21669: "Apple rejected the verification code",
}


@dataclass
class AppleSession:
    adsid: str
    gs_token: str
    token_expiry: datetime | None


def derive_password(password: str, salt: bytes, iterations: int, protocol: str) -> bytes:
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    if protocol == "s2k":
        material = digest
    elif protocol == "s2k_fo":
        material = digest.hex().encode("ascii")
    else:
        raise AppleAuthError(f"Apple asked for an unknown SRP protocol {protocol}")
    return hashlib.pbkdf2_hmac("sha256", material, salt, iterations, dklen=32)


def negotiation_digest(protocol: str, spd: bytes | None, sc: bytes | None) -> bytes:
    """AltSign's running SHA-256 over the protocol negotiation."""
    digest = hashlib.sha256()
    digest.update(b"s2k")
    digest.update(b",")
    digest.update(b"s2k_fo")
    digest.update(b"|")
    if protocol:
        digest.update(protocol.encode("utf-8"))
    digest.update(b"|")
    if spd is not None:
        digest.update(struct.pack("<I", len(spd)))
        digest.update(spd)
    digest.update(b"|")
    if sc is not None:
        digest.update(struct.pack("<I", len(sc)))
        digest.update(sc)
    digest.update(b"|")
    return digest.digest()


def session_hmac(session_key: bytes, label: str) -> bytes:
    return hmac.new(session_key, label.encode("utf-8"), hashlib.sha256).digest()


def decrypt_spd(session_key: bytes, blob: bytes) -> bytes:
    key = session_hmac(session_key, "extra data key:")
    iv = session_hmac(session_key, "extra data iv:")[:16]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(blob) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def encrypt_spd(session_key: bytes, payload: bytes) -> bytes:
    key = session_hmac(session_key, "extra data key:")
    iv = session_hmac(session_key, "extra data iv:")[:16]
    padder = padding.PKCS7(128).padder()
    padded = padder.update(payload) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def decrypt_app_token(sk: bytes, blob: bytes) -> bytes:
    if len(blob) < 35 or blob[:3] != b"XYZ":
        raise AppleAuthError("Apple's app token was not in the expected XYZ envelope")
    iv = blob[3:19]
    body = blob[19:]
    try:
        return AESGCM(sk).decrypt(iv, body, b"XYZ")
    except Exception as exc:  # cryptography raises InvalidTag
        raise AppleAuthError("Could not decrypt Apple's app token") from exc


def encrypt_app_token(sk: bytes, iv: bytes, plaintext: bytes) -> bytes:
    ciphertext = AESGCM(sk).encrypt(iv, plaintext, b"XYZ")
    return b"XYZ" + iv + ciphertext


def app_token_checksum(sk: bytes, adsid: str, apps: list[str]) -> bytes:
    mac = hmac.new(sk, b"apptokens", hashlib.sha256)
    mac.update(adsid.encode("utf-8"))
    for app in apps:
        mac.update(app.encode("utf-8"))
    return mac.digest()


def _loads_plist(blob: bytes) -> dict:
    try:
        data = plistlib.loads(blob)
    except Exception:
        header = (
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        )
        data = plistlib.loads(header + blob)
    if not isinstance(data, dict):
        raise AppleAuthError("Apple returned a plist that was not a dictionary")
    return data


def _as_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise AppleAuthError("expected bytes in Apple's response")


class GsaClient:
    def __init__(self, http=None, *, xcode_version: str = "11.2 (11B41)", timeout: float = 60):
        self.http = http or UrllibHttp()
        self.xcode_version = xcode_version
        self.timeout = timeout

    def authenticate(
        self,
        apple_id: str,
        password: str,
        anisette_fetch,
        code_provider,
        *,
        _depth: int = 0,
    ) -> AppleSession:
        anisette = anisette_fetch()
        user = srp.User(apple_id, b"", hash_alg=srp.SHA256, ng_type=srp.NG_2048)
        _, public_a = user.start_authentication()
        first = self._gsa(
            anisette,
            {"A2k": public_a, "ps": ["s2k", "s2k_fo"], "u": apple_id, "o": "init"},
        )
        protocol = str(first.get("sp") or "")
        salt = first.get("s")
        iterations = first.get("i")
        server_b = first.get("B")
        cookie = first.get("c")
        if not isinstance(salt, bytes) or not isinstance(server_b, bytes) or cookie is None:
            raise AppleAuthError("Apple's login challenge was incomplete")
        if not isinstance(iterations, int):
            raise AppleAuthError("Apple's login challenge had no iteration count")
        user.p = derive_password(password, salt, iterations, protocol)
        m1 = user.process_challenge(salt, server_b)
        if m1 is None:
            raise AppleAuthError("The SRP challenge failed before the password could be checked")
        second = self._gsa(
            anisette_fetch(),
            {"c": cookie, "M1": m1, "u": apple_id, "o": "complete"},
        )
        m2 = second.get("M2")
        # pysrp's verify_session returns None even when the proof matches.
        if not isinstance(m2, bytes):
            raise AppleAuthError("Apple's login proof was missing")
        user.verify_session(m2)
        if not user.authenticated():
            raise AppleAuthError("Apple's login proof did not match")
        session_key = user.get_session_key()
        if not session_key:
            raise AppleAuthError("SRP finished without a session key")
        spd_blob = second.get("spd")
        if not isinstance(spd_blob, bytes):
            raise AppleAuthError("Apple's login response had no session data")
        self._check_negotiation(session_key, protocol, spd_blob, second.get("sc"), second.get("np"))
        spd = _loads_plist(decrypt_spd(session_key, spd_blob))
        adsid = spd.get("adsid")
        idms = spd.get("GsIdmsToken")
        if not isinstance(adsid, str) or not isinstance(idms, str):
            raise AppleAuthError("Apple's session did not include an account id")
        auth_kind = str((second.get("Status") or {}).get("au") or "")
        if auth_kind in {"trustedDeviceSecondaryAuth", "secondaryAuth"}:
            if _depth >= 1:
                raise AppleAuthError(
                    "Apple asked for a verification code again after it was accepted"
                )
            self._second_factor(auth_kind, adsid, idms, anisette_fetch, code_provider)
            return self.authenticate(
                apple_id, password, anisette_fetch, code_provider, _depth=_depth + 1
            )
        sk = spd.get("sk")
        token_cookie = spd.get("c")
        if not isinstance(sk, bytes) or not isinstance(token_cookie, bytes):
            raise AppleAuthError("Apple's session did not include an app-token key")
        return self._exchange_token(adsid, idms, sk, token_cookie, anisette_fetch)

    def _check_negotiation(self, session_key, protocol, spd, sc, np_value) -> None:
        if not isinstance(np_value, bytes):
            raise AppleAuthError("Apple's login response had no negotiation proof")
        sc_bytes = sc if isinstance(sc, bytes) else None
        digest = negotiation_digest(protocol, spd, sc_bytes)
        mac = hmac.new(session_hmac(session_key, "HMAC key:"), digest, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, np_value):
            raise AppleAuthError("Apple's negotiation proof did not match")

    def _exchange_token(self, adsid, idms, sk, cookie, anisette_fetch) -> AppleSession:
        anisette = anisette_fetch()
        apps = [XCODE_APP]
        response = self._gsa(
            anisette,
            {
                "u": adsid,
                "app": apps,
                "c": cookie,
                "t": idms,
                "checksum": app_token_checksum(sk, adsid, apps),
                "o": "apptokens",
            },
        )
        encrypted = response.get("et")
        if not isinstance(encrypted, bytes):
            raise AppleAuthError("Apple did not return an Xcode token")
        token_plist = _loads_plist(decrypt_app_token(sk, encrypted))
        entry = ((token_plist.get("t") or {}).get(XCODE_APP) or {})
        token = entry.get("token")
        if not isinstance(token, str) or not token:
            raise AppleAuthError("Apple's Xcode token was empty")
        expiry = None
        raw_expiry = entry.get("expiry")
        if isinstance(raw_expiry, int):
            expiry = datetime.fromtimestamp(raw_expiry / 1000, tz=UTC)
        return AppleSession(adsid=adsid, gs_token=token, token_expiry=expiry)

    def _second_factor(self, kind, adsid, idms, anisette_fetch, code_provider) -> None:
        anisette = anisette_fetch()
        identity = base64.b64encode(f"{adsid}:{idms}".encode()).decode("ascii")
        headers = {
            "Content-Type": "text/x-xml-plist",
            "User-Agent": "Xcode",
            "Accept": "text/x-xml-plist",
            "Accept-Language": "en-us",
            "X-Apple-App-Info": "com.apple.gs.xcode.auth",
            "X-Xcode-Version": self.xcode_version,
            "X-Apple-Identity-Token": identity,
            **anisette.http_headers(),
        }
        if kind == "secondaryAuth":
            self._sms_code(headers, code_provider)
            return
        triggered = self.http.request(
            "GET",
            "https://gsa.apple.com/auth/verify/trusteddevice",
            headers=headers,
            timeout=self.timeout,
        )
        if triggered.status >= 400:
            raise AppleAuthError(
                f"Apple did not send a trusted-device code (HTTP {triggered.status})"
            )
        code = str(code_provider("trusted_device")).strip().replace(" ", "")
        headers = dict(headers)
        headers["security-code"] = code
        checked = self.http.request(
            "GET",
            "https://gsa.apple.com/grandslam/GsService2/validate",
            headers=headers,
            timeout=self.timeout,
        )
        self._raise_factor_error(checked.body)

    def _sms_code(self, headers, code_provider) -> None:
        body = json_bytes({"phoneNumber": {"id": 1}, "mode": "sms"})
        sent = self.http.request(
            "PUT",
            "https://gsa.apple.com/auth/verify/phone/",
            body=body,
            headers={**headers, "Content-Type": "application/json"},
            timeout=self.timeout,
        )
        if sent.status >= 400:
            raise AppleAuthError(f"Apple did not send an SMS code (HTTP {sent.status})")
        code = str(code_provider("sms")).strip().replace(" ", "")
        payload = {"phoneNumber": {"id": 1}, "mode": "sms", "securityCode": {"code": code}}
        checked = self.http.request(
            "POST",
            "https://gsa.apple.com/auth/verify/phone/securitycode",
            body=json_bytes(payload),
            headers={**headers, "Content-Type": "application/json"},
            timeout=self.timeout,
        )
        if checked.status >= 400:
            raise AppleAuthError("Apple rejected the SMS verification code")

    def _raise_factor_error(self, body: bytes) -> None:
        try:
            data = _loads_plist(body)
        except Exception as exc:
            raise AppleAuthError("Apple's verification response was not a plist") from exc
        code = data.get("ec", 0)
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            code_int = 0
        if code_int != 0:
            message = _ERROR_TEXT.get(code_int, str(data.get("em") or "verification failed"))
            raise AppleAuthError(f"{message} ({code_int})", code_int)

    def _gsa(self, anisette: AnisetteData, parameters: dict) -> dict:
        request = {"cpd": anisette.cpd()}
        request.update(parameters)
        body = plistlib.dumps(
            {"Header": {"Version": "1.0.1"}, "Request": request},
            fmt=plistlib.FMT_XML,
        )
        response = self.http.request(
            "POST",
            GSA_URL,
            body=body,
            headers={
                "Content-Type": "text/x-xml-plist",
                "Accept": "*/*",
                "User-Agent": "akd/1.0 CFNetwork/978.0.7 Darwin/18.7.0",
                "X-MMe-Client-Info": anisette.client_info,
            },
            timeout=self.timeout,
        )
        if response.status >= 400:
            raise AppleAuthError(f"Apple login returned HTTP {response.status}")
        try:
            payload = _loads_plist(response.body)
        except Exception as exc:
            raise AppleAuthError("Apple login response was not a plist") from exc
        inner = payload.get("Response")
        if not isinstance(inner, dict):
            raise AppleAuthError("Apple login response had no Response dictionary")
        status = inner.get("Status") or {}
        code = status.get("ec", 0)
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            code_int = 0
        if code_int != 0:
            text = _ERROR_TEXT.get(code_int) or str(status.get("em") or "Apple login failed")
            raise AppleAuthError(f"{text} ({code_int})", code_int)
        return inner


def json_bytes(payload: dict) -> bytes:
    import json

    return json.dumps(payload).encode("utf-8")


def gs_header(adsid: str, token: str) -> str:
    """Value of X-Apple-GS-Token: base64(adsid + ':' + token), as AltSign sends it."""
    return base64.b64encode(f"{adsid}:{token}".encode()).decode("ascii")
