"""Wi-Fi, address, and battery snapshot for the idle screen."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Hud:
    battery_percent: float | None = None
    charging: bool | None = None
    wifi_up: bool | None = None
    ip: str = ""
    last_refresh: datetime | None = None


def parse_addresses(text: str) -> str:
    """Pick an IPv4 address from `ip -4 -o addr show`.

    A Tailscale address is preferred when one is present, because that is the
    address the owner uses away from home. Otherwise the first non-loopback
    address is shown.
    """
    tailscale = ""
    other = ""
    for line in text.splitlines():
        parts = line.split()
        if "inet" not in parts:
            continue
        iface = parts[1] if len(parts) > 1 else ""
        ip = parts[parts.index("inet") + 1].split("/")[0]
        if ip.startswith("127."):
            continue
        if iface.startswith("tailscale") or ip.startswith("100."):
            if not tailscale:
                tailscale = ip
        elif not other:
            other = ip
    return tailscale or other


def wifi_from_operstate(text: str) -> bool | None:
    value = text.strip().lower()
    if value == "up":
        return True
    if value in {"down", "dormant", "lowerlayerdown"}:
        return False
    return None


class SystemProbe:
    def __init__(self, runner=None, operstate: Path | None = None):
        self.runner = runner
        self.operstate = operstate or Path("/sys/class/net/wlan0/operstate")

    def snapshot(self, battery_percent: float | None, charging: bool | None) -> Hud:
        ip = ""
        if self.runner is not None:
            try:
                ip = parse_addresses(self.runner(["ip", "-4", "-o", "addr", "show"]))
            except Exception:
                ip = ""
        wifi = None
        try:
            wifi = wifi_from_operstate(self.operstate.read_text(encoding="utf-8"))
        except OSError:
            wifi = None
        return Hud(battery_percent=battery_percent, charging=charging, wifi_up=wifi, ip=ip)
