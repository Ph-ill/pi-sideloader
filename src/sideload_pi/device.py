"""Find a paired iPhone through libimobiledevice.

USB is preferred. Wi-Fi is used when the phone is not plugged in and
``idevice_id -l -n`` lists it. That only works after a USB pair, and only
when this libimobiledevice build understands network devices. Neither was
exercised on hardware here.
"""

from __future__ import annotations

from dataclasses import dataclass

from sideload_pi.errors import DeviceError


@dataclass(frozen=True)
class Phone:
    udid: str
    name: str
    via: str  # "usb" or "wifi"


def _udids(text: str) -> list[str]:
    found = []
    for line in text.splitlines():
        token = line.strip()
        if token and not token.startswith("ERROR"):
            found.append(token.split()[0])
    return found


def parse_device_name(text: str) -> str:
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() in {"DeviceName", "ProductName"} and value.strip():
            return value.strip()
    stripped = text.strip()
    return stripped or "iPhone"


class DeviceMonitor:
    def __init__(self, config, runner):
        self.config = config
        self.runner = runner

    def list_udids(self, network: bool) -> list[str]:
        argv = [self.config.idevice_id_bin, "-l"]
        if network:
            argv.append("-n")
        try:
            output = self.runner(argv)
        except Exception:
            return []
        return _udids(output if isinstance(output, str) else output[0])

    def current(self) -> Phone | None:
        usb = self.list_udids(False)
        wifi: list[str] = []
        if self.config.prefer_wifi or not usb:
            wifi = self.list_udids(True)
        if usb and not (self.config.prefer_wifi and wifi):
            udid, via = usb[0], "usb"
        elif wifi:
            udid, via = wifi[0], "wifi"
        else:
            return None
        return Phone(udid=udid, name=self._name(udid, via == "wifi"), via=via)

    def _name(self, udid: str, network: bool) -> str:
        argv = [self.config.ideviceinfo_bin, "-u", udid, "-k", "DeviceName"]
        if network:
            argv.append("-n")
        try:
            output = self.runner(argv)
        except Exception:
            return "iPhone"
        text = output if isinstance(output, str) else ""
        return parse_device_name(text)

    def pair(self, udid: str | None = None) -> str:
        argv = [self.config.idevicepair_bin, "pair"]
        if udid:
            argv.extend(["-u", udid])
        try:
            output = self.runner(argv)
        except Exception as exc:
            raise DeviceError(
                "Could not pair. Unlock the iPhone, tap Trust, and try again."
            ) from exc
        text = output if isinstance(output, str) else ""
        if self.config.enable_wifi_after_pair:
            self._try_wifi_on(udid)
        return text

    def _try_wifi_on(self, udid: str | None) -> None:
        argv = [self.config.idevicepair_bin, "wifi", "on"]
        if udid:
            argv.extend(["-u", udid])
        try:
            self.runner(argv)
        except Exception:
            return
