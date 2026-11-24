"""Refresh scheduler. One pass is `tick`, so tests do not need a thread or a phone."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta

from sideload_pi.battery import Cw2015, should_shutdown
from sideload_pi.device import DeviceMonitor
from sideload_pi.display.epd import LgpioIO, open_panel, partial_refresh_enabled, remembered_version
from sideload_pi.display.policy import RefreshBook, should_repaint
from sideload_pi.display.screens import (
    render_error,
    render_idle,
    render_low_battery,
    render_progress,
    render_stage,
    render_success,
    render_waiting,
    rows_from_apps,
)
from sideload_pi.errors import SideloadError, TwoFactorTimeout
from sideload_pi.installer import Installer
from sideload_pi.layout import Layout, write_secret_text
from sideload_pi.library import Library
from sideload_pi.process import run_command
from sideload_pi.progress import ProgressEvent
from sideload_pi.provisioner import Provisioner
from sideload_pi.scheduling import (
    clock_is_sane,
    format_time,
    ntp_synchronized,
    refresh_due,
    retry_delay_seconds,
    retry_ready,
    utcnow,
    whole_days_remaining,
)
from sideload_pi.signer import Signer
from sideload_pi.system import SystemProbe
from sideload_pi.usb_power import note_phone_on_usb

log = logging.getLogger("sideload_pi")


def submit_code(layout: Layout, code: str, phone_id: str = "") -> None:
    cleaned = code.strip().replace(" ", "")
    prefix = phone_id.strip()
    if prefix:
        if not prefix.isdigit():
            raise SideloadError("The phone id must be a number")
        body = f"{prefix}:{cleaned}"
        digits = cleaned
    elif ":" in cleaned:
        prefix, digits = cleaned.split(":", 1)
        if not prefix.isdigit():
            raise SideloadError("The phone id must be a number")
        body = f"{prefix}:{digits}"
    else:
        digits = cleaned
        body = cleaned
    if not (len(digits) == 6 and digits.isdigit()):
        raise SideloadError("The verification code must be 6 digits")
    layout.ensure()
    write_secret_text(layout.two_fa, body + "\n")


def pop_code(layout: Layout) -> str | None:
    path = layout.two_fa
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    path.unlink(missing_ok=True)
    digits = text.split(":", 1)[-1].replace(" ", "")
    if len(digits) == 6 and digits.isdigit():
        return text
    return None


def power_off() -> None:
    command = ["/bin/systemctl", "poweroff"]
    if os.geteuid() != 0:
        command = ["/usr/bin/sudo", "-n", *command]
    subprocess.run(command, check=False)


def read_live_status(layout: Layout) -> dict | None:
    """Status written by the running daemon. A dead pid is ignored."""
    path = layout.status_path
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return None
    return data


class Daemon:
    def __init__(
        self,
        config,
        layout: Layout,
        library: Library,
        *,
        provisioner: Provisioner | None = None,
        devices: DeviceMonitor | None = None,
        signer: Signer | None = None,
        installer: Installer | None = None,
        panel=None,
        battery: Cw2015 | None = None,
        probe: SystemProbe | None = None,
        shutdown=power_off,
        sleep=time.sleep,
        clock_synced=None,
    ):
        self.config = config
        self.layout = layout
        self.library = library
        self.provisioner = provisioner
        self.devices = devices
        self.signer = signer
        self.installer = installer
        self.panel = panel
        self.battery = battery
        self.probe = probe or SystemProbe()
        self.shutdown = shutdown
        self.sleep = sleep
        self.clock_synced = clock_synced if clock_synced is not None else ntp_synchronized
        self.book = RefreshBook()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopped = False
        self.phase = "idle"
        self.message = ""
        self.detail = ""
        self.percent: int | None = None
        self.active_name = ""
        self.active_bundle = ""
        self.phone = None
        self.hud_battery: float | None = None
        self.hud_charging: bool | None = None
        self._samples: list[float] = []
        self._last_percent: int | None = None
        self._last_paint: datetime | None = None
        self.halted = False
        self._state_mtime: int | None = None
        self.sms_phones: list[dict] = []
        reported = getattr(panel, "version", None) or config.display.version
        self.partial_refresh = partial_refresh_enabled(reported, config.display.allow_partial)

    def run(self) -> None:
        import signal

        self.layout.ensure()
        self.library.load()
        signal.signal(signal.SIGUSR1, lambda *_: self.wake.set())
        signal.signal(signal.SIGTERM, lambda *_: self.stop())
        signal.signal(signal.SIGINT, lambda *_: self.stop())
        log.info("sideload-pi daemon started, library has %s app(s)", len(self.library.apps))
        while not self.stopped:
            try:
                self.tick(utcnow())
            except Exception:
                log.exception("refresh loop hit an unexpected error")
                self.phase = "error"
                self.message = "The refresh loop hit an unexpected error. See the log."
            self.wake.wait(self.config.poll_seconds)
            self.wake.clear()
        if self.panel is not None:
            try:
                self.panel.sleep()
                self.panel.close()
            except Exception:
                log.warning("could not put the e-paper panel to sleep")

    def stop(self) -> None:
        self.stopped = True
        self.wake.set()

    def request_refresh(self, bundle_id: str = "") -> None:
        self.layout.ensure()
        write_secret_text(self.layout.force, bundle_id.strip() + "\n")
        self.wake.set()

    def tick(self, now: datetime) -> None:
        if self.halted:
            return
        self._check_battery(now)
        if self.halted:
            return
        self._reload_library()
        synced = self.clock_synced()
        if not clock_is_sane(now) or synced is False:
            if not clock_is_sane(now):
                detail = "Clock is before 2026-01-01. Waiting for time-sync before any refresh."
            else:
                detail = (
                    "NTP is not synchronized. timedatectl did not report "
                    "NTPSynchronized=yes. Waiting before any refresh."
                )
            self._set("waiting_for_clock", detail)
            self._paint(now)
            return
        self.phone = self.devices.current() if self.devices else None
        if self.phone is not None and self.phone.via == "usb":
            note_phone_on_usb(log)
        forced = self._consume_force()
        due = self._due_apps(now, forced)
        if not due:
            self._set("idle", "")
            self._paint(now)
            return
        if self.phone is None:
            self._set("waiting_for_phone", f"{len(due)} waiting for the iPhone")
            self._paint(now)
            return
        self._refresh(due[0], now)

    def status(self, now: datetime | None = None) -> dict:
        moment = now or utcnow()
        with self.lock:
            apps = []
            for app in self.library.apps.values():
                days = whole_days_remaining(app.expiry, moment)
                apps.append(
                    {
                        "bundle_id": app.bundle_id,
                        "name": app.name,
                        "version": app.version,
                        "days": days,
                        "expiry": app.expiry.strftime("%Y-%m-%dT%H:%M:%SZ") if app.expiry else None,
                        "last_error": app.last_error,
                        "attempts": app.attempts,
                    }
                )
            phone = None
            if self.phone is not None:
                phone = {"udid": self.phone.udid, "name": self.phone.name, "via": self.phone.via}
            return {
                "phase": self.phase,
                "message": self.message,
                "detail": self.detail,
                "percent": self.percent,
                "active_name": self.active_name,
                "active_bundle": self.active_bundle,
                "device": phone,
                "battery_percent": self.hud_battery,
                "charging": self.hud_charging,
                "apps": apps,
                "sms_phones": list(self.sms_phones),
            }

    def wait_for_code(self, kind: str, phones=None) -> str:
        self.sms_phones = [
            {"id": item.id, "label": item.label} for item in (phones or [])
        ]
        if self.sms_phones:
            choices = ", ".join(f"{item['id']} ({item['label']})" for item in self.sms_phones)
            message = f"SMS code. Reply id:123456. {choices}"
        else:
            message = "Enter the 6-digit Apple verification code"
        log.info("waiting for an Apple verification code (%s)", kind)
        self._set("needs_2fa", message)
        self._paint(utcnow())
        deadline = time.monotonic() + self.config.two_fa_timeout_seconds
        while time.monotonic() < deadline:
            if self.stopped:
                raise SideloadError("Stopped while waiting for the verification code")
            code = pop_code(self.layout)
            if code:
                log.info("received an Apple verification code")
                return code
            self.sleep(0.5)
        raise TwoFactorTimeout(
            "Timed out waiting for the Apple verification code. "
            "Run sudo sideload-pi 2fa 123456 or submit it on the web page, then refresh again."
        )

    def _due_apps(self, now: datetime, forced: str | None):
        due = []
        for app in self.library.apps.values():
            if forced is not None and forced not in ("", app.bundle_id):
                continue
            wanted = forced is not None or refresh_due(app.expiry, now, self.config.refresh_margin)
            if not wanted:
                continue
            if forced is None and not retry_ready(app.next_retry_at, now):
                continue
            due.append(app)
        due.sort(key=lambda app: app.expiry or datetime.min.replace(tzinfo=UTC))
        return due

    def _consume_force(self) -> str | None:
        path = self.layout.force
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8").strip()
        path.unlink(missing_ok=True)
        return text

    def _refresh(self, app, now: datetime) -> None:
        self.active_bundle = app.bundle_id
        self.active_name = app.name
        self.percent = None
        self._last_percent = None
        self._last_paint = None
        phone = self.phone
        assert phone is not None
        try:
            if phone.via == "usb" and self.devices is not None:
                self.devices.pair(phone.udid)
            self._set("provisioning", "Refreshing the Apple development profile")
            self._paint(now)
            assert self.provisioner is not None
            material = self.provisioner.provision(
                app.bundle_id,
                app.name,
                phone.udid,
                phone.name,
                self.wait_for_code,
                now,
            )
            self._set("signing", "Re-signing the IPA")
            self._paint(utcnow())
            signed = self.layout.work / f"{app.bundle_id}.signed.ipa"
            assert self.signer is not None
            self.signer.sign(
                app.ipa_path(self.layout),
                key=material.key_path,
                certificate=material.cert_path,
                profile=material.profile_path,
                output=signed,
                strip_extensions=self.config.strip_extensions,
                strip_watch=self.config.strip_watch,
            )
            self._set("installing", "Starting the install")
            self.percent = 0
            self._paint(utcnow())

            def on_event(event) -> None:
                if not isinstance(event, ProgressEvent):
                    return
                moment = utcnow()
                if self.partial_refresh:
                    min_delta, min_seconds = 5, 2.0
                else:
                    min_delta = self.config.display.full_only_percent
                    min_seconds = float(self.config.display.full_only_seconds)
                if not should_repaint(
                    self._last_percent,
                    event.percent,
                    self._last_paint,
                    moment,
                    min_delta=min_delta,
                    min_seconds=min_seconds,
                ):
                    self.detail = event.stage
                    if event.percent is not None:
                        self.percent = event.percent
                    return
                self.phase = "installing"
                self.detail = event.stage
                if event.percent is not None:
                    self.percent = event.percent
                self._last_percent = self.percent
                self._last_paint = moment
                self._paint(moment)

            assert self.installer is not None
            self.installer.install(
                signed,
                phone.udid,
                app.bundle_id,
                phone.via == "wifi",
                on_event,
            )
            self.library.mark_success(app.bundle_id, material.expiry, utcnow())
            days = whole_days_remaining(material.expiry, utcnow())
            self._set("success", f"{app.name} installed")
            self.detail = f"{days}d" if days is not None else ""
            self.percent = 100
            log.info("installed %s, profile expires %s", app.bundle_id, material.expiry.isoformat())
            self._paint(utcnow())
        except SideloadError as exc:
            self._fail(app.bundle_id, str(exc), now)
        except Exception as exc:
            log.exception("refresh failed for %s", app.bundle_id)
            self._fail(app.bundle_id, f"Refresh failed: {exc}", now)

    def _fail(self, bundle_id: str, message: str, now: datetime) -> None:
        delay = retry_delay_seconds(
            self.library.apps[bundle_id].attempts + 1,
            self.config.retry_base_seconds,
            self.config.retry_cap_seconds,
        )
        self.library.mark_failure(bundle_id, message, now, delay=timedelta(seconds=delay))
        self._set("error", message)
        log.warning("refresh failed for %s: %s (retry in %ss)", bundle_id, message, delay)
        self._paint(utcnow())

    def _check_battery(self, now: datetime) -> None:
        if self.battery is None:
            self.hud_battery = None
            self.hud_charging = None
            return
        reading = self.battery.read()
        self.hud_charging = reading.charging
        if reading.percent is None:
            self.hud_battery = None
            if reading.raw_error:
                log.warning("battery gauge: %s", reading.raw_error)
            return
        self.hud_battery = reading.percent
        self._samples.append(reading.percent)
        self._samples = self._samples[-10:]
        if should_shutdown(
            self._samples,
            charging=reading.charging,
            enabled=self.config.battery.shutdown_on_low_battery,
            threshold=self.config.battery.low_percent,
            confirm=self.config.battery.confirm_readings,
        ):
            self._set("low_battery", "Shutting down on low battery")
            self._paint(now)
            log.warning("low battery %.1f%%, shutting down", reading.percent)
            self.halted = True
            self.stopped = True
            self.shutdown()

    def _reload_library(self) -> None:
        path = self.layout.state_path
        try:
            current = path.stat().st_mtime_ns if path.exists() else None
        except OSError:
            current = None
        if current is None or current == self._state_mtime:
            return
        try:
            self.library.load()
        except Exception:
            log.exception("could not reload the library; keeping the in-memory copy")
            return
        self._state_mtime = current

    def _set(self, phase: str, message: str) -> None:
        self.phase = phase
        self.message = message
        self._publish()

    def _publish(self) -> None:
        try:
            payload = self.status()
            payload["pid"] = os.getpid()
            payload["updated_at"] = format_time(utcnow())
            write_secret_text(self.layout.status_path, json.dumps(payload, indent=2) + "\n")
        except Exception:
            log.exception("could not write the live status file")

    def _paint(self, now: datetime) -> None:
        image = self._image(now)
        if self.panel is None or image is None:
            return
        full = self.book.choose(
            self.phase,
            now,
            every=self.config.display.full_refresh_every,
            minutes=self.config.display.full_refresh_minutes,
            partial_ok=self.partial_refresh,
        )
        try:
            self.panel.show(image, full)
        except Exception:
            log.exception("e-paper update failed")

    def _image(self, now: datetime):
        hud = self.probe.snapshot(self.hud_battery, self.hud_charging)
        hud.last_refresh = self.library.last_refresh
        phase = self.phase
        if phase == "installing":
            return render_progress(self.active_name, self.detail, self.percent)
        if phase == "signing":
            return render_stage("Signing", self.active_name, self.message or "Re-signing the IPA")
        if phase == "provisioning":
            return render_stage("Apple", self.active_name, self.message or "Refreshing the profile")
        if phase == "success":
            days = whole_days_remaining(
                self.library.apps[self.active_bundle].expiry if self.active_bundle in self.library.apps else None,
                now,
            )
            return render_success(self.active_name, days)
        if phase == "error":
            return render_error(self.message)
        if phase == "needs_2fa":
            return render_stage(
                "Apple ID code",
                "Six digits",
                "sudo sideload-pi 2fa 123456 or the web page. The code is not stored.",
            )
        if phase == "waiting_for_phone":
            return render_waiting(max(1, len(self._due_apps(now, None))))
        if phase == "low_battery":
            return render_low_battery(self.hud_battery, True)
        notice = ""
        if self.hud_battery is not None and self.hud_battery < self.config.battery.low_percent:
            notice = "battery low, plug in the UPS"
        rows = rows_from_apps(list(self.library.apps.values()), now, self.config.refresh_margin.total_seconds() / 86400)
        return render_idle(rows, hud, now, notice)


def _gpio_is_high(pin: int):
    import lgpio

    handle = lgpio.gpiochip_open(0)
    lgpio.gpio_claim_input(handle, pin)

    def read() -> bool:
        return lgpio.gpio_read(handle, pin) == 1

    return read


def open_battery(config) -> Cw2015 | None:
    if not config.battery.enabled:
        return None
    try:
        import smbus
    except ImportError:
        log.warning("python3-smbus is not installed; the battery gauge is off")
        return None
    try:
        bus = smbus.SMBus(config.battery.i2c_bus)
    except OSError as exc:
        log.warning("I2C bus %s is not available (%s)", config.battery.i2c_bus, exc)
        return None
    power = None
    if config.battery.power_detect_enabled:
        try:
            power = _gpio_is_high(config.battery.power_gpio_bcm)
        except Exception as exc:
            log.warning("GPIO %s power detect is unavailable (%s)", config.battery.power_gpio_bcm, exc)
    return Cw2015(bus, config.battery.address, power)


def open_display(config, layout: Layout):
    if not config.display.enabled:
        return None
    # A confirmed display-test writes this file. It wins over the config,
    # including the shipped default of V4. auto with no file is not probed:
    # V1's init never reads BUSY, so a guess must not be saved.
    saved = remembered_version(layout.display_version)
    version = saved or config.display.version
    if version == "auto":
        log.warning("display.version is auto and display-test has not confirmed a driver")
        return None
    try:
        panel = open_panel(
            lambda _version: LgpioIO(config),
            version,
            rotate=config.display.rotate,
            saved=saved,
            busy_timeout=20,
        )
    except Exception:
        log.exception("e-paper did not initialise; the daemon will keep running")
        return None
    return panel


def build(config, *, connect_hardware: bool = True) -> Daemon:
    layout = Layout(config.root)
    layout.ensure()
    library = Library(layout)
    library.load()
    runner = run_command
    panel = open_display(config, layout) if connect_hardware else None
    battery = open_battery(config) if connect_hardware else None
    return Daemon(
        config,
        layout,
        library,
        provisioner=Provisioner(config, layout, library),
        devices=DeviceMonitor(config, runner),
        signer=Signer(config, runner),
        installer=Installer(config, runner),
        panel=panel,
        battery=battery,
        probe=SystemProbe(runner),
    )
