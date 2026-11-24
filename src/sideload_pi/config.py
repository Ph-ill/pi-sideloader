"""JSON configuration. Secrets never live in this file."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sideload_pi.errors import ConfigError

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
EPD_VERSIONS = frozenset({"V1", "V2", "V3", "V4", "auto"})


def _as_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    return value


def _as_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def _as_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value


def parse_address(value: object) -> int:
    if isinstance(value, str):
        text = value.strip().lower()
        base = 16 if text.startswith("0x") else 10
        try:
            parsed = int(text, base)
        except ValueError as exc:
            raise ConfigError("battery.address must be an integer or hex string") from exc
        return parsed
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ConfigError("battery.address must be an integer or hex string")


@dataclass
class WebConfig:
    enabled: bool = True
    bind: str = "127.0.0.1"
    port: int = 8741
    max_upload_mb: int = 200
    allow_remote: bool = False


@dataclass
class DisplayConfig:
    enabled: bool = True
    version: str = "V4"
    # False forces throttled full refreshes even on V2/V3/V4. V1 never partial-refreshes.
    allow_partial: bool = True
    full_refresh_every: int = 8
    full_refresh_minutes: int = 30
    # Progress-bar paints when partial refresh is unavailable.
    full_only_percent: int = 20
    full_only_seconds: int = 15
    rotate: int = 90
    spi_bus: int = 0
    spi_device: int = 0
    dc_pin: int = 25
    rst_pin: int = 17
    busy_pin: int = 24
    cs_pin: int = 8


@dataclass
class BatteryConfig:
    enabled: bool = True
    i2c_bus: int = 1
    address: int = 0x62
    power_gpio_bcm: int = 4
    power_detect_enabled: bool = False
    low_percent: float = 8.0
    shutdown_on_low_battery: bool = False
    confirm_readings: int = 2


@dataclass
class Config:
    refresh_margin: timedelta = timedelta(days=1)
    poll_seconds: int = 20
    retry_base_seconds: int = 300
    retry_cap_seconds: int = 6 * 3600
    max_apps: int = 3
    max_app_ids_per_window: int = 10
    app_id_window_days: int = 7
    profile_lifetime_days: int = 7
    strip_extensions: bool = True
    strip_watch: bool = True
    sha256_only: bool = True
    prefer_wifi: bool = False
    enable_wifi_after_pair: bool = True
    xcode_version: str = "11.2 (11B41)"
    client_id: str = "XABBG36SBA"
    protocol_version: str = "QH65B2"
    anisette_url: str = ""
    machine_name: str = "sideload-pi"
    two_fa_timeout_seconds: int = 600
    http_timeout_seconds: int = 60
    root: Path = Path("/var/lib/sideload-pi")
    zsign_bin: str = "zsign"
    ideviceinstaller_bin: str = "ideviceinstaller"
    idevice_id_bin: str = "idevice_id"
    ideviceinfo_bin: str = "ideviceinfo"
    idevicepair_bin: str = "idevicepair"
    web: WebConfig = field(default_factory=WebConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    battery: BatteryConfig = field(default_factory=BatteryConfig)

    def validate(self) -> None:
        if self.refresh_margin.total_seconds() < 0:
            raise ConfigError("refresh_margin_days cannot be negative")
        if self.poll_seconds < 5:
            raise ConfigError("poll_seconds must be at least 5")
        if self.retry_base_seconds < 1 or self.retry_cap_seconds < self.retry_base_seconds:
            raise ConfigError("retry intervals are invalid")
        if self.max_apps < 1:
            raise ConfigError("max_apps must be at least 1")
        if self.max_app_ids_per_window < 1 or self.app_id_window_days < 1:
            raise ConfigError("App ID window settings are invalid")
        if self.two_fa_timeout_seconds < 30:
            raise ConfigError("two_fa_timeout_seconds must be at least 30")
        if self.display.version not in EPD_VERSIONS:
            raise ConfigError("display.version must be V1, V2, V3, V4, or auto")
        if self.display.full_refresh_every < 1:
            raise ConfigError("display.full_refresh_every must be at least 1")
        if not 1 <= self.display.full_only_percent <= 100:
            raise ConfigError("display.full_only_percent must be between 1 and 100")
        if self.display.full_only_seconds < 1:
            raise ConfigError("display.full_only_seconds must be at least 1")
        if self.display.rotate not in (0, 90, 180, 270):
            raise ConfigError("display.rotate must be 0, 90, 180, or 270")
        if not 1 <= self.web.port <= 65535:
            raise ConfigError("web.port is out of range")
        if self.web.max_upload_mb < 1:
            raise ConfigError("web.max_upload_mb must be at least 1")
        if self.web.bind not in LOOPBACK and not self.web.allow_remote:
            raise ConfigError(
                "web.bind is not loopback. Set web.allow_remote to true only if you "
                "accept exposing the token-protected UI on that address."
            )
        if not 0 <= self.battery.low_percent <= 100:
            raise ConfigError("battery.low_percent must be between 0 and 100")
        if not 0 <= self.battery.address <= 0x7F:
            raise ConfigError("battery.address is not a 7-bit I2C address")
        if self.battery.confirm_readings < 1:
            raise ConfigError("battery.confirm_readings must be at least 1")
        if self.battery.shutdown_on_low_battery and not self.battery.power_detect_enabled:
            raise ConfigError(
                "battery.shutdown_on_low_battery requires battery.power_detect_enabled. "
                "GPIO4 power detection needs the two pads on the UPS-Lite soldered; "
                "without that, a floating pin could shut the Pi down by mistake."
            )


def _section(data: dict, name: str) -> dict:
    raw = data.get(name, {})
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be an object")
    return raw


def config_from_dict(data: dict) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config must be a JSON object")
    web_raw = _section(data, "web")
    display_raw = _section(data, "display")
    battery_raw = _section(data, "battery")
    web = WebConfig(
        enabled=_as_bool(web_raw["enabled"], "web.enabled") if "enabled" in web_raw else True,
        bind=_as_str(web_raw["bind"], "web.bind") if "bind" in web_raw else "127.0.0.1",
        port=_as_int(web_raw["port"], "web.port") if "port" in web_raw else 8741,
        max_upload_mb=(
            _as_int(web_raw["max_upload_mb"], "web.max_upload_mb")
            if "max_upload_mb" in web_raw
            else 200
        ),
        allow_remote=(
            _as_bool(web_raw["allow_remote"], "web.allow_remote")
            if "allow_remote" in web_raw
            else False
        ),
    )
    display = DisplayConfig(
        enabled=(
            _as_bool(display_raw["enabled"], "display.enabled")
            if "enabled" in display_raw
            else True
        ),
        version=display_raw.get("version", "V4"),
        allow_partial=(
            _as_bool(display_raw["allow_partial"], "display.allow_partial")
            if "allow_partial" in display_raw
            else True
        ),
        full_refresh_every=(
            _as_int(display_raw["full_refresh_every"], "display.full_refresh_every")
            if "full_refresh_every" in display_raw
            else 8
        ),
        full_refresh_minutes=(
            _as_int(display_raw["full_refresh_minutes"], "display.full_refresh_minutes")
            if "full_refresh_minutes" in display_raw
            else 30
        ),
        rotate=_as_int(display_raw["rotate"], "display.rotate") if "rotate" in display_raw else 90,
        full_only_percent=(
            _as_int(display_raw["full_only_percent"], "display.full_only_percent")
            if "full_only_percent" in display_raw
            else 20
        ),
        full_only_seconds=(
            _as_int(display_raw["full_only_seconds"], "display.full_only_seconds")
            if "full_only_seconds" in display_raw
            else 15
        ),
        spi_bus=(
            _as_int(display_raw["spi_bus"], "display.spi_bus") if "spi_bus" in display_raw else 0
        ),
        spi_device=(
            _as_int(display_raw["spi_device"], "display.spi_device")
            if "spi_device" in display_raw
            else 0
        ),
        dc_pin=_as_int(display_raw["dc_pin"], "display.dc_pin") if "dc_pin" in display_raw else 25,
        rst_pin=(
            _as_int(display_raw["rst_pin"], "display.rst_pin") if "rst_pin" in display_raw else 17
        ),
        busy_pin=(
            _as_int(display_raw["busy_pin"], "display.busy_pin")
            if "busy_pin" in display_raw
            else 24
        ),
        cs_pin=_as_int(display_raw["cs_pin"], "display.cs_pin") if "cs_pin" in display_raw else 8,
    )
    battery = BatteryConfig(
        enabled=(
            _as_bool(battery_raw["enabled"], "battery.enabled")
            if "enabled" in battery_raw
            else True
        ),
        i2c_bus=(
            _as_int(battery_raw["i2c_bus"], "battery.i2c_bus") if "i2c_bus" in battery_raw else 1
        ),
        address=parse_address(battery_raw["address"]) if "address" in battery_raw else 0x62,
        power_gpio_bcm=(
            _as_int(battery_raw["power_gpio_bcm"], "battery.power_gpio_bcm")
            if "power_gpio_bcm" in battery_raw
            else 4
        ),
        power_detect_enabled=(
            _as_bool(battery_raw["power_detect_enabled"], "battery.power_detect_enabled")
            if "power_detect_enabled" in battery_raw
            else False
        ),
        low_percent=(
            float(battery_raw["low_percent"]) if "low_percent" in battery_raw else 8.0
        ),
        shutdown_on_low_battery=(
            _as_bool(battery_raw["shutdown_on_low_battery"], "battery.shutdown_on_low_battery")
            if "shutdown_on_low_battery" in battery_raw
            else False
        ),
        confirm_readings=(
            _as_int(battery_raw["confirm_readings"], "battery.confirm_readings")
            if "confirm_readings" in battery_raw
            else 2
        ),
    )
    margin_days = data.get("refresh_margin_days", 1)
    if isinstance(margin_days, bool) or not isinstance(margin_days, (int, float)):
        raise ConfigError("refresh_margin_days must be a number")
    cfg = Config(
        refresh_margin=timedelta(days=float(margin_days)),
        poll_seconds=_as_int(data["poll_seconds"], "poll_seconds") if "poll_seconds" in data else 20,
        retry_base_seconds=(
            _as_int(data["retry_base_seconds"], "retry_base_seconds")
            if "retry_base_seconds" in data
            else 300
        ),
        retry_cap_seconds=(
            _as_int(data["retry_cap_seconds"], "retry_cap_seconds")
            if "retry_cap_seconds" in data
            else 6 * 3600
        ),
        max_apps=_as_int(data["max_apps"], "max_apps") if "max_apps" in data else 3,
        max_app_ids_per_window=(
            _as_int(data["max_app_ids_per_window"], "max_app_ids_per_window")
            if "max_app_ids_per_window" in data
            else 10
        ),
        app_id_window_days=(
            _as_int(data["app_id_window_days"], "app_id_window_days")
            if "app_id_window_days" in data
            else 7
        ),
        profile_lifetime_days=(
            _as_int(data["profile_lifetime_days"], "profile_lifetime_days")
            if "profile_lifetime_days" in data
            else 7
        ),
        strip_extensions=(
            _as_bool(data["strip_extensions"], "strip_extensions")
            if "strip_extensions" in data
            else True
        ),
        strip_watch=(
            _as_bool(data["strip_watch"], "strip_watch") if "strip_watch" in data else True
        ),
        sha256_only=(
            _as_bool(data["sha256_only"], "sha256_only") if "sha256_only" in data else True
        ),
        prefer_wifi=_as_bool(data["prefer_wifi"], "prefer_wifi") if "prefer_wifi" in data else False,
        enable_wifi_after_pair=(
            _as_bool(data["enable_wifi_after_pair"], "enable_wifi_after_pair")
            if "enable_wifi_after_pair" in data
            else True
        ),
        xcode_version=(
            _as_str(data["xcode_version"], "xcode_version")
            if "xcode_version" in data
            else "11.2 (11B41)"
        ),
        client_id=_as_str(data["client_id"], "client_id") if "client_id" in data else "XABBG36SBA",
        protocol_version=(
            _as_str(data["protocol_version"], "protocol_version")
            if "protocol_version" in data
            else "QH65B2"
        ),
        anisette_url=str(data.get("anisette_url", "") or ""),
        machine_name=(
            _as_str(data["machine_name"], "machine_name")
            if "machine_name" in data
            else "sideload-pi"
        ),
        two_fa_timeout_seconds=(
            _as_int(data["two_fa_timeout_seconds"], "two_fa_timeout_seconds")
            if "two_fa_timeout_seconds" in data
            else 600
        ),
        http_timeout_seconds=(
            _as_int(data["http_timeout_seconds"], "http_timeout_seconds")
            if "http_timeout_seconds" in data
            else 60
        ),
        root=Path(data["root"]) if "root" in data else Path("/var/lib/sideload-pi"),
        zsign_bin=str(data.get("zsign_bin", "zsign")),
        ideviceinstaller_bin=str(data.get("ideviceinstaller_bin", "ideviceinstaller")),
        idevice_id_bin=str(data.get("idevice_id_bin", "idevice_id")),
        ideviceinfo_bin=str(data.get("ideviceinfo_bin", "ideviceinfo")),
        idevicepair_bin=str(data.get("idevicepair_bin", "idevicepair")),
        web=web,
        display=display,
        battery=battery,
    )
    cfg.validate()
    return cfg


def load_config(path: Path | None = None) -> Config:
    chosen = path
    if chosen is None:
        env = os.environ.get("SIDELOAD_PI_CONFIG")
        if env:
            chosen = Path(env)
    if chosen is None or not chosen.exists():
        cfg = Config()
        cfg.validate()
        return cfg
    try:
        data = json.loads(chosen.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"could not parse {chosen}: {exc}") from exc
    return config_from_dict(data)
