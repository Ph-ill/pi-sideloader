"""Fetch anisette headers from a remote anisette-v3-server.

Apple's ADI library ships as armeabi-v7a, arm64, and x86. A Pi Zero (ARMv6)
cannot run it, so this device calls a server the owner runs elsewhere.
Keep that server's identity on disk. A new identity on every boot makes Apple
ask for a verification code every time.

Since 12 October 2026, GsService2 init fails with -80045 unless the User-Agent
and X-MMe-Client-Info are the pair from the client that produced the anisette
data (AltSign#61, AltStore#1828). Dadoum's anisette-v3-server publishes that
pair at GET /v3/client_info. GET / still returns X-MMe-Client-Info from the
v1 device description, which is a different string, so it is not used for the
identity. The one-time password still comes from GET /.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sideload_pi.errors import AnisetteError
from sideload_pi.http_client import UrllibHttp

_ALIASES = {
    "md": ("X-Apple-I-MD", "oneTimePassword", "one_time_password"),
    "md_m": ("X-Apple-I-MD-M", "machineID", "machine_id"),
    "md_lu": ("X-Apple-I-MD-LU", "localUserID", "local_user_id"),
    "rinfo": ("X-Apple-I-MD-RINFO", "routingInfo", "routing_info"),
    "device_id": ("X-Mme-Device-Id", "X-Mme-Device-ID", "deviceUniqueIdentifier", "device_id"),
    "serial": ("X-Apple-I-SRL-NO", "deviceSerialNumber", "serial"),
    "client_info": ("X-MMe-Client-Info", "X-Mme-Client-Info", "deviceDescription", "client_info"),
    "locale": ("X-Apple-Locale", "locale"),
    "timezone": ("X-Apple-I-TimeZone", "timeZone", "timezone"),
    "client_time": ("X-Apple-I-Client-Time", "date"),
}

@dataclass(frozen=True)
class AnisetteData:
    md: str
    md_m: str
    md_lu: str
    rinfo: str
    device_id: str
    serial: str
    client_info: str
    locale: str
    timezone: str
    client_time: str
    # From GET /v3/client_info. Empty only for fixtures that never talk to GSA.
    user_agent: str = ""

    def http_headers(self) -> dict[str, str]:
        return {
            "X-Apple-I-MD": self.md,
            "X-Apple-I-MD-M": self.md_m,
            "X-Apple-I-MD-LU": self.md_lu,
            "X-Apple-I-MD-RINFO": self.rinfo,
            "X-Mme-Device-Id": self.device_id,
            "X-Apple-I-SRL-NO": self.serial,
            "X-MMe-Client-Info": self.client_info,
            "X-Apple-I-Client-Time": self.client_time,
            "X-Apple-I-TimeZone": self.timezone,
            "X-Apple-Locale": self.locale,
        }

    def cpd(self) -> dict:
        """String-only client dictionary from AltSign#61.

        Booleans and the extra anisette headers (RINFO included) are rejected
        together with a mismatched User-Agent. ``loc`` is the literal en_US
        that function sends, not the anisette locale header.
        """
        return {
            "bootstrap": "true",
            "icscrec": "true",
            "pbe": "false",
            "prkgen": "true",
            "svct": "iCloud",
            "loc": "en_US",
            "X-Apple-I-MD": self.md,
            "X-Apple-I-MD-M": self.md_m,
            "X-Mme-Device-Id": self.device_id,
        }


def _pick(payload: dict, names: tuple[str, ...]) -> str:
    lowered = {str(key).lower(): value for key, value in payload.items()}
    for name in names:
        if name in payload and payload[name] not in (None, ""):
            return str(payload[name])
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return str(value)
    return ""


def parse_anisette(payload: dict, *, now: datetime | None = None) -> AnisetteData:
    if not isinstance(payload, dict):
        raise AnisetteError("anisette response was not a JSON object")
    moment = now or datetime.now(UTC)
    md_lu = _pick(payload, _ALIASES["md_lu"])
    device_id = _pick(payload, _ALIASES["device_id"])
    data = AnisetteData(
        md=_pick(payload, _ALIASES["md"]),
        md_m=_pick(payload, _ALIASES["md_m"]),
        md_lu=md_lu or device_id,
        rinfo=_pick(payload, _ALIASES["rinfo"]) or "17106176",
        device_id=device_id,
        serial=_pick(payload, _ALIASES["serial"]) or "0",
        client_info=_pick(payload, _ALIASES["client_info"]),
        locale=_pick(payload, _ALIASES["locale"]) or "en_US",
        timezone=_pick(payload, _ALIASES["timezone"]) or "UTC",
        client_time=_pick(payload, _ALIASES["client_time"]) or moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    missing = [
        label
        for label, value in (
            ("X-Apple-I-MD", data.md),
            ("X-Apple-I-MD-M", data.md_m),
            ("device id", data.device_id),
        )
        if not value
    ]
    if missing:
        raise AnisetteError("anisette response is missing " + ", ".join(missing))
    return data


class AnisetteClient:
    def __init__(self, url: str, http=None, timeout: float = 20):
        self.url = url.rstrip("/")
        self.http = http or UrllibHttp()
        self.timeout = timeout

    def fetch(self, *, now: datetime | None = None) -> AnisetteData:
        if not self.url:
            raise AnisetteError(
                "No anisette server is configured. A Pi Zero cannot generate anisette "
                "data locally because Apple's ADI library has no ARMv6 build. Set "
                "anisette_url to an anisette-v3-server you run, then try again."
            )
        identity = self._get_json(self.url + "/v3/client_info")
        client_info = str(identity.get("client_info") or "")
        user_agent = str(identity.get("user_agent") or "")
        if not client_info or not user_agent:
            raise AnisetteError(
                "anisette /v3/client_info did not include client_info and user_agent. "
                "Since 12 October 2026 Apple rejects GsService2 unless both match the "
                "client that produced the anisette data."
            )
        payload = self._get_json(self.url + "/")
        data = parse_anisette(payload, now=now)
        # The v1 body still carries X-MMe-Client-Info, and it is not the v3 pair.
        return replace(data, client_info=client_info, user_agent=user_agent)

    def resync(self, md_data: str) -> None:
        """Ask the anisette server to resynchronize after Apple's hsc 434.

        anisette-v3-server's app.d has no route that accepts X-Apple-I-MD-DATA.
        Provision's older anisette server accepts GET /reprovision when
        allow-remote-reprovisioning is on. POST /v3/resync is tried first for a
        server that grew that route; a 404 falls through to GET /reprovision.
        If neither answers, the owner has to restart the v3 server so it
        re-provisions locally. This does not invent a successful resync.
        """
        if not md_data:
            raise AnisetteError("Apple's resync request had no X-Apple-I-MD-DATA")
        posted = self.http.request(
            "POST",
            self.url + "/v3/resync",
            body=json.dumps({"X-Apple-I-MD-DATA": md_data}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        if 200 <= posted.status < 300:
            return
        reprovisioned = self.http.request(
            "GET", self.url + "/reprovision", timeout=self.timeout
        )
        if 200 <= reprovisioned.status < 300:
            return
        raise AnisetteError(
            "Apple asked the anisette server to resynchronize (hsc 434), but it "
            f"refused POST /v3/resync (HTTP {posted.status}) and GET /reprovision "
            f"(HTTP {reprovisioned.status}). Dadoum's anisette-v3-server has no "
            "remote resync route. Restart that server so it re-provisions, then "
            "sign in again."
        )

    def _get_json(self, url: str) -> dict:
        response = self.http.request("GET", url, timeout=self.timeout)
        if response.status != 200:
            raise AnisetteError(f"anisette server returned HTTP {response.status} for {url}")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AnisetteError(f"anisette server did not return JSON for {url}") from exc
        if not isinstance(payload, dict):
            raise AnisetteError(f"anisette response was not a JSON object for {url}")
        return payload
