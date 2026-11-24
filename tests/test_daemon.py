import logging
from datetime import UTC, datetime, timedelta

from sideload_pi.config import Config
from sideload_pi.daemon import Daemon
from sideload_pi.device import Phone
from sideload_pi.errors import SideloadError
from sideload_pi.layout import Layout, write_secret_text
from sideload_pi.library import AppRecord, Library
from sideload_pi.progress import ProgressEvent
from sideload_pi.provisioner import Material
from sideload_pi.usb_power import reset_note_for_tests

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class _Devices:
    def __init__(self, phone):
        self.phone = phone
        self.paired = []

    def current(self):
        return self.phone

    def pair(self, udid):
        self.paired.append(udid)
        return "SUCCESS"


class _Provisioner:
    def __init__(self, expiry, error=None):
        self.expiry = expiry
        self.error = error
        self.calls = []

    def provision(self, bundle_id, name, udid, device_name, code_provider, now):
        self.calls.append(bundle_id)
        if self.error:
            raise self.error
        return Material(
            profile_path=self.expiry and bundle_id,
            key_path=bundle_id,
            cert_path=bundle_id,
            expiry=self.expiry,
            free=True,
        )


class _Signer:
    def sign(self, ipa, *, key, certificate, profile, output, strip_extensions, strip_watch):
        output.write_bytes(b"signed")


class _Installer:
    def __init__(self, percents):
        self.percents = percents

    def install(self, ipa, udid, bundle_id, network, on_event):
        for percent in self.percents:
            on_event(ProgressEvent("Install", "ExtractingPackage", percent, False, percent == 100))


class _Panel:
    def __init__(self, version="V4"):
        self.version = version
        self.frames = []

    def show(self, image, full):
        self.frames.append(full)


def _daemon(tmp_path, **kwargs):
    config = Config(root=tmp_path)
    if "version" in kwargs:
        config.display.version = kwargs.pop("version")
    layout = Layout(tmp_path)
    layout.ensure()
    library = Library(layout)
    library.apps["com.example.notes"] = AppRecord(
        "com.example.notes",
        "Notes",
        "1",
        "notes.ipa",
        expiry=kwargs.pop("expiry", NOW + timedelta(days=6)),
    )
    panel = kwargs.pop("panel", _Panel())
    clock_synced = kwargs.pop("clock_synced", lambda: True)
    return Daemon(
        config,
        layout,
        library,
        provisioner=kwargs.get("provisioner"),
        devices=_Devices(kwargs.get("phone")),
        signer=_Signer(),
        installer=_Installer(kwargs.get("percents", [10, 12, 40, 100])),
        panel=panel,
        probe=_Probe(),
        clock_synced=clock_synced,
    )


class _Probe:
    def snapshot(self, battery, charging):
        from sideload_pi.system import Hud

        return Hud(battery_percent=battery, charging=charging, wifi_up=True, ip="192.168.1.9")


def test_not_due_idle_and_waiting_for_the_phone(tmp_path):
    daemon = _daemon(tmp_path, phone=None, provisioner=_Provisioner(NOW + timedelta(days=7)))
    daemon.tick(NOW)
    assert daemon.phase == "idle"
    assert daemon.provisioner.calls == []
    daemon.library.apps["com.example.notes"].expiry = None
    daemon.tick(NOW)
    assert daemon.phase == "waiting_for_phone"


def test_refresh_installs_and_then_backs_off(tmp_path):
    phone = Phone("UDID", "Pocket", "usb")
    expiry = NOW + timedelta(days=7)
    panel = _Panel("V4")
    daemon = _daemon(tmp_path, phone=phone, provisioner=_Provisioner(expiry), panel=panel, expiry=None)
    daemon.tick(NOW)
    assert daemon.phase == "success"
    assert phone.udid in daemon.devices.paired
    assert daemon.library.apps["com.example.notes"].expiry == expiry
    assert any(frame is False for frame in panel.frames)

    daemon.provisioner.error = SideloadError("profile rejected")
    daemon.library.apps["com.example.notes"].expiry = NOW
    daemon.library.save()
    daemon.tick(NOW)
    assert daemon.phase == "error"
    assert daemon.library.apps["com.example.notes"].attempts == 1
    daemon.tick(NOW + timedelta(seconds=10))
    assert daemon.phase == "idle"
    daemon.tick(NOW + timedelta(seconds=301))
    assert daemon.provisioner.calls[-1] == "com.example.notes"


def test_v1_throttles_progress_to_full_refreshes(tmp_path):
    phone = Phone("UDID", "Pocket", "usb")
    panel = _Panel("V1")
    daemon = _daemon(
        tmp_path,
        phone=phone,
        provisioner=_Provisioner(NOW + timedelta(days=7)),
        panel=panel,
        expiry=None,
        version="V1",
        percents=[10, 12, 40, 100],
    )
    assert daemon.partial_refresh is False
    before = len(panel.frames)
    daemon.tick(NOW)
    progress_frames = panel.frames[before:]
    assert all(frame is True for frame in progress_frames)
    # 10% paints, 12% is inside the 20-point / 15-second throttle, 40% and 100% paint.
    assert progress_frames.count(True) >= 2


def test_force_file_ignores_backoff(tmp_path):
    phone = Phone("UDID", "Pocket", "wifi")
    daemon = _daemon(
        tmp_path,
        phone=phone,
        provisioner=_Provisioner(NOW + timedelta(days=7)),
        expiry=NOW + timedelta(days=6),
    )
    daemon.library.apps["com.example.notes"].next_retry_at = NOW + timedelta(hours=2)
    write_secret_text(daemon.layout.force, "com.example.notes\n")
    daemon.tick(NOW)
    assert daemon.phase == "success"


def test_unsynced_clock_does_not_decide_a_refresh(tmp_path):
    phone = Phone("UDID", "Pocket", "usb")
    provisioner = _Provisioner(NOW + timedelta(days=7))
    daemon = _daemon(tmp_path, phone=phone, provisioner=provisioner, expiry=None)
    daemon.tick(datetime(2020, 1, 1, tzinfo=UTC))
    assert daemon.phase == "waiting_for_clock"
    assert provisioner.calls == []


def test_post_2026_clock_without_ntp_does_not_refresh(tmp_path):
    phone = Phone("UDID", "Pocket", "usb")
    provisioner = _Provisioner(NOW + timedelta(days=7))
    daemon = _daemon(
        tmp_path,
        phone=phone,
        provisioner=provisioner,
        expiry=None,
        clock_synced=lambda: False,
    )
    daemon.tick(NOW)
    assert daemon.phase == "waiting_for_clock"
    assert "NTP" in daemon.message
    assert provisioner.calls == []


def test_a_cli_add_survives_the_daemons_next_save(tmp_path):
    from tests.support import make_ipa

    daemon = _daemon(tmp_path, phone=None)
    daemon.library.save()
    other = Library(daemon.layout)
    other.load()
    ipa = make_ipa(tmp_path / "extra.ipa", bundle="com.example.extra", name="Extra")
    other.add_ipa(
        ipa,
        max_apps=3,
        strip_extensions=True,
        now=NOW,
        window=timedelta(days=7),
        app_id_limit=10,
    )
    daemon.library.mark_success("com.example.notes", NOW + timedelta(days=7), NOW)
    saved = Library(daemon.layout)
    saved.load()
    assert "com.example.extra" in saved.apps
    assert saved.apps["com.example.notes"].expiry == NOW + timedelta(days=7)


def test_usb_power_note_is_logged_once(tmp_path, caplog):
    reset_note_for_tests()
    daemon = _daemon(tmp_path, phone=Phone("UDID", "Pocket", "usb"))
    with caplog.at_level(logging.INFO, logger="sideload_pi"):
        daemon.tick(NOW)
        daemon.tick(NOW)
    notes = [rec.message for rec in caplog.records if "cannot turn off VBUS" in rec.message]
    assert len(notes) == 1
