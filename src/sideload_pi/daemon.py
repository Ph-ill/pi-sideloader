"""Refresh scheduler. One pass is `tick`, so tests do not need a thread or a phone."""

from __future__ import annotations

import logging
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


def submit_code(layout: Layout, code: str) -> None:
    cleaned = code.strip().replace(" ", "")
    if not (len(cleaned) == 6 and cleaned.isdigit()):
        raise SideloadError("The verification code must be 6 digits")
    layout.ensure()
    write_secret_text(layout.two_fa, cleaned + "\n")


def pop_code(layout: Layout) -> str | None:
    path = layout.two_fa
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip().replace(" ", "")
    path.unlink(missing_ok=True)
    if len(text) == 6 and text.isdigit():
        return text
    return None


def power_off() -> None:
    subprocess.run(["/bin/systemctl", "poweroff"], check=False)


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
            }

    def wait_for_code(self, kind: str) -> str:
        log.info("waiting for an Apple verification code (%s)", kind)
        self._set("needs_2fa", "Enter the 6-digit Apple verification code")
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

    def _set(self, phase: str, message: str) -> None:
        self.phase = phase
        self.message = message

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
    saved = remembered_version(layout.display_version)
    try:
        panel = open_panel(
            lambda _version: LgpioIO(config),
            config.display.version,
            rotate=config.display.rotate,
            saved=saved,
            busy_timeout=8 if config.display.version == "auto" else 20,
        )
    except Exception:
        log.exception("e-paper did not initialise; the daemon will keep running")
        return None
    if config.display.version == "auto":
        layout.display_version.write_text(panel.version + "\n")
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
