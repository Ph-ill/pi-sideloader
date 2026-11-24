"""Fetch anisette headers from a remote anisette-v3-server (its v1 GET /).

Apple's ADI library ships as armeabi-v7a, arm64, and x86. A Pi Zero (ARMv6)
cannot run it, so this device calls a server the owner runs elsewhere.
Dadoum's anisette-v3-server answers GET / with the header set AltServer-Linux
already consumes. Keep that server's identity on disk. A new identity on every
boot makes Apple ask for a verification code every time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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

_DEFAULT_CLIENT = (
    "<iMac11,3> <Mac OS X;10.15.6;19G2021> "
    "<com.apple.AuthKit/1 (com.apple.dt.Xcode/3594.4.19)>"
)


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
        headers = self.http_headers()
        if self.rinfo.isdigit():
            headers["X-Apple-I-MD-RINFO"] = int(self.rinfo)  # type: ignore[assignment]
        return {
            "bootstrap": True,
            "icscrec": True,
            "pbe": False,
            "prkgen": True,
            "svct": "iCloud",
            "loc": self.locale,
            **headers,
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
        client_info=_pick(payload, _ALIASES["client_info"]) or _DEFAULT_CLIENT,
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
        response = self.http.request("GET", self.url + "/", timeout=self.timeout)
        if response.status != 200:
            raise AnisetteError(f"anisette server returned HTTP {response.status}")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AnisetteError("anisette server did not return JSON") from exc
        return parse_anisette(payload, now=now)
