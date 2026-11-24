"""Read the XML plist embedded in a CMS-signed .mobileprovision."""

from __future__ import annotations

import plistlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from sideload_pi.errors import SideloadError
from sideload_pi.scheduling import ensure_aware


@dataclass
class ProfileInfo:
    name: str
    expiry: datetime
    created: datetime | None
    team_id: str
    app_id: str
    devices: list[str]
    free: bool | None

    @property
    def lifetime(self) -> timedelta | None:
        if self.created is None:
            return None
        return self.expiry - self.created


def parse_mobileprovision(blob: bytes) -> ProfileInfo:
    start = blob.find(b"<?xml")
    end = blob.find(b"</plist>")
    if start < 0 or end < 0:
        raise SideloadError("provisioning profile did not contain an XML plist")
    end += len(b"</plist>")
    try:
        data = plistlib.loads(blob[start:end])
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise SideloadError(f"could not parse provisioning profile: {exc}") from exc
    if not isinstance(data, dict):
        raise SideloadError("provisioning profile plist was not a dictionary")
    expiry = data.get("ExpirationDate")
    if not isinstance(expiry, datetime):
        raise SideloadError("provisioning profile has no ExpirationDate")
    created = data.get("CreationDate")
    if not isinstance(created, datetime):
        created = None
    teams = data.get("TeamIdentifier") or []
    team_id = teams[0] if isinstance(teams, list) and teams else ""
    entitlements = data.get("Entitlements") or {}
    app_id = ""
    if isinstance(entitlements, dict):
        raw = entitlements.get("application-identifier") or ""
        app_id = raw if isinstance(raw, str) else ""
    devices = data.get("ProvisionedDevices") or []
    if not isinstance(devices, list):
        devices = []
    free_flag = data.get("IsFreeProvisioningProfile")
    free: bool | None
    if isinstance(free_flag, bool):
        free = free_flag
    elif created is not None and (ensure_aware(expiry) - ensure_aware(created)) <= timedelta(days=8):
        free = True
    else:
        free = None
    return ProfileInfo(
        name=str(data.get("Name") or ""),
        expiry=ensure_aware(expiry),
        created=ensure_aware(created) if created else None,
        team_id=str(team_id),
        app_id=app_id,
        devices=[str(item) for item in devices],
        free=free,
    )
