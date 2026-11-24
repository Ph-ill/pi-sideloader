"""developerservices2.apple.com calls used to provision a free Personal Team.

Endpoints, the client id ``XABBG36SBA``, protocol ``QH65B2``, and the result
codes below are the ones AltSign documents and sends. The session header is
``X-Apple-GS-Token: base64(adsid + ':' + xcode token)`` plus fresh anisette
headers. Responses are checked for ``resultCode``. A live Apple account was
not used to exercise this module.
"""

from __future__ import annotations

import json
import plistlib
import uuid
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from sideload_pi.anisette import AnisetteData
from sideload_pi.errors import AppleAPIError
from sideload_pi.gsa import gs_header
from sideload_pi.http_client import UrllibHttp

BASE = "https://developerservices2.apple.com/services/QH65B2/"
# Free-team certificate list and revoke. SideSign uses this v1 JSON API
# (POST + X-HTTP-Method-Override), not a QH65B2 plist action.
CERTS_V1 = "https://developerservices2.apple.com/services/v1/certificates"

_RESULT_TEXT = {
    35: "Apple rejected an identifier or name",
    7460: "This account already has the maximum number of development certificates",
    3250: "Apple rejected the certificate request",
    7252: "That certificate does not exist",
    8101: "That provisioning profile does not exist",
    8201: "That App ID does not exist",
    9100: "That App ID does not exist",
    9115: "That App ID does not exist",
    9120: "Apple's App ID limit for this account has been reached (10 per 7 days on a free account)",
    9401: "That bundle identifier is not available",
    9412: "That bundle identifier is not valid",
}


@dataclass
class Team:
    team_id: str
    name: str
    kind: str
    status: str


@dataclass
class DeviceRecord:
    udid: str
    name: str


@dataclass
class IssuedCertificate:
    der: bytes
    serial: str = ""
    cert_id: str = ""


@dataclass
class ListedCertificate:
    cert_id: str
    serial: str
    machine_name: str
    machine_id: str


@dataclass
class AppIdRecord:
    app_id_id: str
    identifier: str
    name: str


def choose_team(teams: list[Team]) -> Team:
    """Prefer an explicit Free team, then an Individual team, then any active one."""

    def rank(team: Team) -> tuple[int, str]:
        status = (team.status or "active").lower()
        if status not in {"active", ""}:
            return (9, team.team_id)
        kind = (team.kind or "").lower()
        if kind == "free":
            return (0, team.team_id)
        if kind == "individual":
            return (1, team.team_id)
        return (2, team.team_id)

    if not teams:
        raise AppleAPIError("This Apple ID has no developer team")
    return sorted(teams, key=rank)[0]


class DeveloperClient:
    def __init__(
        self,
        *,
        adsid: str,
        gs_token: str,
        client_id: str = "XABBG36SBA",
        protocol: str = "QH65B2",
        xcode_version: str = "11.2 (11B41)",
        http=None,
        timeout: float = 60,
    ):
        self.adsid = adsid
        self.gs_token = gs_token
        self.client_id = client_id
        self.protocol = protocol
        self.xcode_version = xcode_version
        self.http = http or UrllibHttp()
        self.timeout = timeout

    def list_teams(self, anisette: AnisetteData) -> list[Team]:
        data = self._post("listTeams.action", {}, anisette)
        teams = []
        for item in data.get("teams") or []:
            if not isinstance(item, dict):
                continue
            teams.append(
                Team(
                    team_id=str(item.get("teamId") or ""),
                    name=str(item.get("name") or ""),
                    kind=str(item.get("type") or ""),
                    status=str(item.get("status") or "active"),
                )
            )
        return [team for team in teams if team.team_id]

    def list_devices(self, team_id: str, anisette: AnisetteData) -> list[DeviceRecord]:
        data = self._post("ios/listDevices.action", {"teamId": team_id}, anisette)
        found = []
        for item in data.get("devices") or []:
            if isinstance(item, dict) and item.get("deviceNumber"):
                found.append(
                    DeviceRecord(
                        udid=str(item["deviceNumber"]),
                        name=str(item.get("name") or ""),
                    )
                )
        return found

    def add_device(self, team_id: str, udid: str, name: str, anisette: AnisetteData) -> None:
        self._post(
            "ios/addDevice.action",
            {"teamId": team_id, "deviceNumber": udid, "name": name, "DTDK_Platform": "ios"},
            anisette,
        )

    def list_app_ids(self, team_id: str, anisette: AnisetteData) -> list[AppIdRecord]:
        data = self._post("ios/listAppIds.action", {"teamId": team_id}, anisette)
        found = []
        for item in data.get("appIds") or []:
            if isinstance(item, dict) and item.get("appIdId") and item.get("identifier"):
                found.append(
                    AppIdRecord(
                        app_id_id=str(item["appIdId"]),
                        identifier=str(item["identifier"]),
                        name=str(item.get("name") or ""),
                    )
                )
        return found

    def add_app_id(
        self, team_id: str, bundle_id: str, name: str, anisette: AnisetteData
    ) -> AppIdRecord:
        data = self._post(
            "ios/addAppId.action",
            {
                "teamId": team_id,
                "identifier": bundle_id,
                "name": name,
                "appIdName": name,
            },
            anisette,
        )
        app = data.get("appId") if isinstance(data.get("appId"), dict) else data
        app_id = str((app or {}).get("appIdId") or "")
        if not app_id:
            raise AppleAPIError("Apple did not return an App ID id")
        return AppIdRecord(app_id_id=app_id, identifier=bundle_id, name=name)

    def submit_csr(
        self,
        team_id: str,
        csr_der_b64: str,
        machine_id: str,
        machine_name: str,
        anisette: AnisetteData,
    ) -> IssuedCertificate:
        data = self._post(
            "ios/submitDevelopmentCSR.action",
            {
                "teamId": team_id,
                "csrContent": csr_der_b64,
                "machineId": machine_id,
                "machineName": machine_name,
            },
            anisette,
        )
        cert_request = data.get("certRequest") or {}
        certificate = cert_request.get("certificate")
        if not isinstance(certificate, bytes) or not certificate:
            raise AppleAPIError("Apple did not return a development certificate")
        serial = str(cert_request.get("serialNumber") or cert_request.get("serialNum") or "")
        cert_id = str(cert_request.get("certificateId") or cert_request.get("certRequestId") or "")
        return IssuedCertificate(der=certificate, serial=serial, cert_id=cert_id)

    def list_development_certs(self, team_id: str, anisette: AnisetteData) -> list[ListedCertificate]:
        query = urlencode(
            {"teamId": team_id, "filter[certificateType]": "IOS_DEVELOPMENT"}
        )
        payload = self._services("GET", CERTS_V1, query, anisette)
        found = []
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            attributes = item.get("attributes") or {}
            cert_id = str(item.get("id") or "")
            if not cert_id:
                continue
            found.append(
                ListedCertificate(
                    cert_id=cert_id,
                    serial=str(attributes.get("serialNumber") or ""),
                    machine_name=str(attributes.get("machineName") or ""),
                    machine_id=str(attributes.get("machineId") or ""),
                )
            )
        return found

    def revoke_certificate(self, team_id: str, cert_id: str, anisette: AnisetteData) -> None:
        if not cert_id:
            raise AppleAPIError("refusing to revoke a certificate with no id")
        query = urlencode({"teamId": team_id})
        self._services("DELETE", f"{CERTS_V1}/{quote(cert_id, safe='')}", query, anisette)

    def download_profile(self, team_id: str, app_id_id: str, anisette: AnisetteData) -> bytes:
        data = self._post(
            "ios/downloadTeamProvisioningProfile.action",
            {"teamId": team_id, "appIdId": app_id_id, "DTDK_Platform": "ios"},
            anisette,
        )
        profile = data.get("provisioningProfile") or {}
        blob = profile.get("provisioningProfile") if isinstance(profile, dict) else None
        if isinstance(blob, bytes) and blob:
            return blob
        raise AppleAPIError("Apple did not return a provisioning profile")

    def _post(self, action: str, extra: dict, anisette: AnisetteData) -> dict:
        body_dict = {
            "clientId": self.client_id,
            "protocolVersion": self.protocol,
            "requestId": str(uuid.uuid4()).upper(),
            "userLocale": ["en_US"],
            **extra,
        }
        url = f"{BASE}{action}?clientId={self.client_id}"
        headers = {
            "Content-Type": "text/x-xml-plist",
            "Accept": "text/x-xml-plist",
            "User-Agent": "Xcode",
            "Accept-Language": "en-us",
            "X-Apple-App-Info": "com.apple.gs.xcode.auth",
            "X-Xcode-Version": self.xcode_version,
            "X-Apple-I-Identity-Id": self.adsid,
            "X-Apple-GS-Token": gs_header(self.adsid, self.gs_token),
            **anisette.http_headers(),
        }
        response = self.http.request(
            "POST",
            url,
            body=plistlib.dumps(body_dict, fmt=plistlib.FMT_XML),
            headers=headers,
            timeout=self.timeout,
        )
        if response.status in {401, 403}:
            raise AppleAPIError(f"Apple rejected the saved login (HTTP {response.status})", 401)
        if response.status >= 400:
            raise AppleAPIError(f"Apple developer services returned HTTP {response.status}")
        try:
            data = plistlib.loads(response.body)
        except Exception as exc:
            raise AppleAPIError("Apple developer services did not return a plist") from exc
        if not isinstance(data, dict):
            raise AppleAPIError("Apple developer services returned an unexpected plist")
        code = data.get("resultCode", 0)
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            code_int = 0
        if code_int != 0:
            text = _RESULT_TEXT.get(code_int) or str(
                data.get("resultString") or data.get("userString") or "Apple request failed"
            )
            raise AppleAPIError(f"{text} (result {code_int})", code_int)
        return data

    def _services(self, method: str, url: str, query: str, anisette: AnisetteData) -> dict:
        headers = {
            "Content-Type": "application/vnd.api+json",
            "Accept": "application/vnd.api+json",
            "User-Agent": "Xcode",
            "Accept-Language": "en-us",
            "X-Apple-App-Info": "com.apple.gs.xcode.auth",
            "X-Xcode-Version": self.xcode_version,
            "X-Apple-I-Identity-Id": self.adsid,
            "X-Apple-GS-Token": gs_header(self.adsid, self.gs_token),
            "X-HTTP-Method-Override": method,
            **anisette.http_headers(),
        }
        response = self.http.request(
            "POST",
            url,
            body=json.dumps({"urlEncodedQueryParams": query}).encode("utf-8"),
            headers=headers,
            timeout=self.timeout,
        )
        if response.status in {401, 403}:
            raise AppleAPIError(f"Apple rejected the saved login (HTTP {response.status})", 401)
        if response.status >= 400:
            raise AppleAPIError(
                f"Apple certificate services returned HTTP {response.status} for {method}"
            )
        if not response.body.strip():
            return {}
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppleAPIError("Apple certificate services did not return JSON") from exc
        if not isinstance(payload, dict):
            raise AppleAPIError("Apple certificate services returned an unexpected body")
        errors = payload.get("errors")
        if errors:
            detail = errors[0].get("detail") if isinstance(errors[0], dict) else str(errors[0])
            raise AppleAPIError(f"Apple certificate services: {detail}")
        return payload
