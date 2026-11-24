"""Inspect an IPA without resigning it. FairPlay-encrypted apps cannot be re-signed."""

from __future__ import annotations

import plistlib
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from sideload_pi.errors import SideloadError

_BUNDLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{2,}$")


@dataclass
class IpaInfo:
    path: Path
    bundle_id: str
    name: str
    version: str
    extension_ids: list[str] = field(default_factory=list)
    watch: bool = False
    fairplay: bool = False

    @property
    def safe_filename(self) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", self.bundle_id)
        return f"{cleaned}.ipa"


def _load_plist(blob: bytes) -> dict:
    try:
        data = plistlib.loads(blob)
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise SideloadError(f"could not read an Info.plist: {exc}") from exc
    if not isinstance(data, dict):
        raise SideloadError("Info.plist was not a dictionary")
    return data


def _display_name(info: dict, bundle_id: str) -> str:
    for key in ("CFBundleDisplayName", "CFBundleName"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return bundle_id.rsplit(".", 1)[-1]


def inspect_ipa(path: Path) -> IpaInfo:
    if not path.is_file():
        raise SideloadError(f"IPA not found: {path}")
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise SideloadError(f"{path.name} is not a zip/IPA") from exc
    with archive:
        names = archive.namelist()
        apps = [
            name
            for name in names
            if name.startswith("Payload/")
            and name.endswith(".app/Info.plist")
            and name.count("/") == 2
        ]
        if len(apps) != 1:
            raise SideloadError(
                f"{path.name} should contain exactly one Payload/*.app (found {len(apps)})"
            )
        info = _load_plist(archive.read(apps[0]))
        bundle_id = info.get("CFBundleIdentifier")
        if not isinstance(bundle_id, str) or not _BUNDLE_ID.match(bundle_id):
            raise SideloadError(f"{path.name} has no usable CFBundleIdentifier")
        extensions: list[str] = []
        for name in names:
            if "/PlugIns/" in name and name.endswith(".appex/Info.plist"):
                plugin = _load_plist(archive.read(name))
                plugin_id = plugin.get("CFBundleIdentifier")
                if isinstance(plugin_id, str):
                    extensions.append(plugin_id)
        watch = any("/Watch/" in name for name in names)
        fairplay = any("/SC_Info/" in name for name in names)
        version = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or ""
        if not isinstance(version, str):
            version = str(version)
        return IpaInfo(
            path=path,
            bundle_id=bundle_id,
            name=_display_name(info, bundle_id),
            version=version,
            extension_ids=extensions,
            watch=watch,
            fairplay=fairplay,
        )


def assert_signable(info: IpaInfo) -> None:
    if info.fairplay:
        raise SideloadError(
            f"{info.name} looks FairPlay-encrypted (it contains SC_Info). "
            "App Store downloads cannot be re-signed. Use the IPA you built yourself."
        )
