"""Parse ideviceinstaller status lines.

ideviceinstaller (libimobiledevice) prints, from its status callback:

    Install: ExtractingPackage (15%)
    Install: Complete
    ERROR: Install failed. Got error "Name" with code 0x........: description

Lines are carriage-return updated. PercentComplete is included only when the
device sent it. Stage names without a percent are still reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Order observed in installation_proxy. Used only when the tool omits a percent,
# so the bar still moves. Real percents from the device always win.
_STAGE_FALLBACK = {
    "CreatingStagingDirectory": 5,
    "ExtractingPackage": 15,
    "InspectingPackage": 20,
    "TakingInstallLock": 25,
    "PreflightingApplication": 30,
    "VerifyingApplication": 40,
    "CreatingContainer": 50,
    "InstallingApplication": 60,
    "PostflightingApplication": 70,
    "SandboxingApplication": 80,
    "GeneratingApplicationMap": 90,
    "Complete": 100,
}

_STATUS = re.compile(
    r"(?P<command>Install|Upgrade):\s+(?P<stage>[A-Za-z0-9_]+)"
    r"(?:\s+\((?P<percent>\d{1,3})%\))?"
)
_ERROR = re.compile(
    r'ERROR:\s+(?P<command>\w+)\s+failed\.\s+Got error\s+"(?P<name>[^"]+)"'
    r'(?:\s+with code\s+(?P<code>0x[0-9A-Fa-f]+):\s*(?P<description>.*))?'
)


@dataclass(frozen=True)
class ProgressEvent:
    command: str
    stage: str
    percent: int | None
    approximate: bool
    complete: bool


@dataclass(frozen=True)
class InstallErrorEvent:
    command: str
    name: str
    code: str | None
    description: str

    def message(self) -> str:
        detail = self.description.strip() if self.description else self.name
        if self.code:
            return f"{self.command} failed: {detail} ({self.code})"
        return f"{self.command} failed: {detail}"


def _chunks(text: str) -> list[str]:
    parts: list[str] = []
    for line in text.splitlines():
        parts.extend(piece.strip() for piece in line.split("\r") if piece.strip())
    if "\r" in text and not text.endswith(("\n", "\r")):
        tail = text.split("\r")[-1].strip()
        if tail and (not parts or parts[-1] != tail):
            parts.append(tail)
    return parts


def parse_installer_output(text: str) -> list[ProgressEvent | InstallErrorEvent]:
    events: list[ProgressEvent | InstallErrorEvent] = []
    for piece in _chunks(text):
        error = _ERROR.search(piece)
        if error:
            events.append(
                InstallErrorEvent(
                    command=error.group("command"),
                    name=error.group("name"),
                    code=error.group("code"),
                    description=(error.group("description") or "").strip(),
                )
            )
            continue
        match = _STATUS.search(piece)
        if not match:
            continue
        stage = match.group("stage")
        raw = match.group("percent")
        if raw is not None:
            percent: int | None = max(0, min(100, int(raw)))
            approximate = False
        else:
            percent = _STAGE_FALLBACK.get(stage)
            approximate = percent is not None and stage != "Complete"
        events.append(
            ProgressEvent(
                command=match.group("command"),
                stage=stage,
                percent=percent,
                approximate=approximate,
                complete=stage == "Complete",
            )
        )
    return events


def display_percent(event: ProgressEvent) -> int | None:
    return event.percent
