from pathlib import Path

import pytest

from sideload_pi.config import Config
from sideload_pi.device import DeviceMonitor, parse_device_name
from sideload_pi.errors import InstallError, SignError
from sideload_pi.installer import Installer, parse_installed_bundles
from sideload_pi.process import CommandError, run_command
from sideload_pi.signer import Signer
from sideload_pi.system import parse_addresses


def test_usb_is_preferred_unless_wifi_is_asked_for(tmp_path):
    config = Config(root=tmp_path)
    calls = []

    def runner(argv, on_text=None, timeout=None):
        calls.append(argv)
        if argv[0] == "idevice_id" and argv[-1] == "-n":
            return "wifi-udid\n"
        if argv[0] == "idevice_id":
            return "usb-udid\n"
        return "DeviceName: Pocket\n"

    phone = DeviceMonitor(config, runner).current()
    assert phone.udid == "usb-udid"
    assert phone.via == "usb"
    assert phone.name == "Pocket"
    config.prefer_wifi = True
    phone = DeviceMonitor(config, runner).current()
    assert phone.via == "wifi"
    assert parse_device_name("no colon here") == "no colon here"


def test_installer_upgrades_an_app_that_is_already_there(tmp_path):
    config = Config(root=tmp_path)
    seen = []

    def runner(argv, on_text=None, timeout=None):
        seen.append(list(argv))
        if "list" in argv:
            return "<plist><key>CFBundleIdentifier</key><string>com.example.notes</string></plist>"
        if on_text:
            on_text("Upgrade: InstallingApplication (60%)\rUpgrade: Complete\n")
        return ""

    Installer(config, runner).install(Path("app.ipa"), "UDID", "com.example.notes", True, lambda event: None)
    assert seen[0][1] == "-n"
    assert "upgrade" in seen[1]
    assert parse_installed_bundles("com.example.notes, Notes, 1\n").__contains__("com.example.notes")


def test_install_error_is_raised_from_the_tool_output(tmp_path):
    config = Config(root=tmp_path)

    def runner(argv, on_text=None, timeout=None):
        if "list" in argv:
            return ""
        raise CommandError(argv, 1, 'ERROR: Install failed. Got error "APIInternalError" with code 0xe8000001: boom\n')

    with pytest.raises(InstallError):
        Installer(config, runner).install(Path("app.ipa"), "UDID", "com.example.notes", False, lambda event: None)


def test_zsign_argv_has_no_password_and_requests_sha256_only(tmp_path):
    config = Config(root=tmp_path)
    seen = []

    def runner(argv, on_text=None, timeout=None):
        seen.append(argv)
        return ""

    Signer(config, runner).sign(
        Path("in.ipa"),
        key=Path("k.pem"),
        certificate=Path("c.pem"),
        profile=Path("p.mobileprovision"),
        output=Path("out.ipa"),
        strip_extensions=True,
        strip_watch=True,
    )
    assert "-2" in seen[0]
    assert "-E" in seen[0]
    assert "-W" in seen[0]
    assert "-p" not in seen[0]
    assert seen[0][-1] == "in.ipa"

    def broken(argv, on_text=None, timeout=None):
        raise CommandError(argv, 1, "line\nbad profile\n")

    with pytest.raises(SignError, match="bad profile"):
        Signer(config, broken).sign(
            Path("in.ipa"),
            key=Path("k.pem"),
            certificate=Path("c.pem"),
            profile=Path("p.mobileprovision"),
            output=Path("out.ipa"),
            strip_extensions=True,
            strip_watch=True,
        )


def test_tailscale_address_wins():
    text = "2: wlan0 inet 192.168.1.20/24\n3: tailscale0 inet 100.64.1.8/32\n"
    assert parse_addresses(text) == "100.64.1.8"


def test_command_timeout_kills_a_hung_process():
    with pytest.raises(CommandError, match="timed out"):
        run_command(["python3", "-c", "import time; time.sleep(30)"], timeout=0.4)
