"""Install a signed IPA with ideviceinstaller and surface its percentages."""

from __future__ import annotations

from pathlib import Path

from sideload_pi.errors import InstallError
from sideload_pi.process import CommandError
from sideload_pi.progress import InstallErrorEvent, parse_installer_output


def parse_installed_bundles(text: str) -> set[str]:
    """Pull bundle ids out of `ideviceinstaller list` text or XML."""
    found: set[str] = set()
    if "<plist" in text or "<key>CFBundleIdentifier</key>" in text:
        parts = text.split("<key>CFBundleIdentifier</key>")
        for part in parts[1:]:
            start = part.find("<string>")
            end = part.find("</string>")
            if start >= 0 and end > start:
                found.add(part[start + len("<string>") : end].strip())
        return found
    for line in text.splitlines():
        piece = line.strip()
        if not piece or piece.lower().startswith("total"):
            continue
        token = piece.split(",", 1)[0].strip().split()[0]
        if token.count(".") >= 1 and " " not in token:
            found.add(token)
    return found


class Installer:
    def __init__(self, config, runner):
        self.config = config
        self.runner = runner

    def installed_bundles(self, udid: str, network: bool) -> set[str]:
        argv = [
            self.config.ideviceinstaller_bin,
            "-u",
            udid,
            "list",
            "--user",
            "--xml",
        ]
        if network:
            argv.insert(1, "-n")
        try:
            output = self.runner(argv, timeout=120)
        except CommandError:
            return set()
        return parse_installed_bundles(output if isinstance(output, str) else "")

    def install(self, ipa: Path, udid: str, bundle_id: str, network: bool, on_event) -> None:
        present = bundle_id in self.installed_bundles(udid, network)
        command = "upgrade" if present else "install"
        argv = [self.config.ideviceinstaller_bin, "-u", udid, command, str(ipa)]
        if network:
            argv.insert(1, "-n")
        collected: list[str] = []

        def on_text(text: str) -> None:
            collected[:] = [text]
            for event in parse_installer_output(text):
                on_event(event)

        try:
            self.runner(argv, on_text, timeout=1200)
        except CommandError as exc:
            self._raise(exc.output, command)
        except OSError as exc:
            raise InstallError(
                "ideviceinstaller is not installed. The install script adds it from apt."
            ) from exc
        output = collected[0] if collected else ""
        for event in parse_installer_output(output):
            if isinstance(event, InstallErrorEvent):
                raise InstallError(event.message())

    def _raise(self, output: str, command: str) -> None:
        for event in parse_installer_output(output):
            if isinstance(event, InstallErrorEvent):
                raise InstallError(event.message())
        tail = output.strip().splitlines()
        detail = tail[-1] if tail else f"{command} failed"
        raise InstallError(detail)
