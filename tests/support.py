"""Shared fixtures for unit tests. No phone, GPIO, or network."""

from __future__ import annotations

import io
import plistlib
import zipfile
from pathlib import Path


def make_ipa(
    path: Path,
    bundle: str = "com.example.notes",
    name: str = "Notes",
    version: str = "1.0",
    extensions: tuple[str, ...] = (),
    watch: bool = False,
    fairplay: bool = False,
) -> Path:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = {
            "CFBundleIdentifier": bundle,
            "CFBundleDisplayName": name,
            "CFBundleShortVersionString": version,
        }
        archive.writestr(f"Payload/{name}.app/Info.plist", plistlib.dumps(info))
        for ext in extensions:
            plugin = {"CFBundleIdentifier": ext, "CFBundleDisplayName": ext}
            archive.writestr(
                f"Payload/{name}.app/PlugIns/{ext}.appex/Info.plist",
                plistlib.dumps(plugin),
            )
        if watch:
            archive.writestr(
                "Payload/Watch/Watch.app/Info.plist",
                plistlib.dumps({"CFBundleIdentifier": bundle + ".watch"}),
            )
        if fairplay:
            archive.writestr(f"Payload/{name}.app/SC_Info/x.sinf", b"nope")
    path.write_bytes(buffer.getvalue())
    return path
