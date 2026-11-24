"""Command line. `sideload-pi login` is the step that handles the first 2FA prompt."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from sideload_pi import __version__
from sideload_pi.config import load_config
from sideload_pi.daemon import build, pop_code, read_live_status, submit_code
from sideload_pi.display.epd import DRIVERS, LgpioIO, open_panel
from sideload_pi.display.screens import render_test_pattern, write_gallery, write_test_patterns
from sideload_pi.errors import SideloadError
from sideload_pi.layout import Layout
from sideload_pi.logging_setup import configure_logging
from sideload_pi.scheduling import utcnow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sideload-pi")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--version", action="store_true")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("daemon")
    sub.add_parser("status")
    sub.add_parser("enable")
    sub.add_parser("login")
    code = sub.add_parser("2fa")
    code.add_argument("digits")
    add = sub.add_parser("add")
    add.add_argument("ipa", type=Path)
    remove = sub.add_parser("remove")
    remove.add_argument("bundle_id")
    refresh = sub.add_parser("refresh")
    refresh.add_argument("--app", default="")
    preview = sub.add_parser("preview")
    preview.add_argument("--out", type=Path, required=True)
    sub.add_parser("pair")
    probe = sub.add_parser("display-test")
    probe.add_argument("driver", nargs="?", choices=list(DRIVERS))
    probe.add_argument(
        "--preview",
        type=Path,
        default=None,
        help="write the pattern PNG here instead of talking to the panel",
    )
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if not args.command:
        parser.print_help()
        return 2
    config = load_config(args.config)
    if args.command == "preview":
        write_gallery(args.out, utcnow())
        write_test_patterns(args.out)
        print(f"wrote screens to {args.out}")
        return 0
    if args.command == "display-test" and args.preview is not None:
        version = args.driver or config.display.version
        if version == "auto":
            print(
                "Pass V1, V2, V3, or V4. Auto is not used for the test pattern.",
                file=sys.stderr,
            )
            return 1
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        render_test_pattern(version).save(args.preview)
        print(f"wrote {args.preview}")
        return 0
    layout = Layout(config.root)
    layout.ensure()
    configure_logging(layout.logs / "sideload-pi.log")
    try:
        if args.command == "daemon":
            return _daemon(config)
        if args.command == "status":
            return _status(config)
        if args.command == "enable":
            return _enable(config)
        if args.command == "login":
            return _login(config)
        if args.command == "2fa":
            submit_code(layout, args.digits)
            print("code submitted")
            return 0
        if args.command == "add":
            return _add(config, args.ipa)
        if args.command == "remove":
            return _remove(config, args.bundle_id)
        if args.command == "refresh":
            return _refresh(config, args.app)
        if args.command == "pair":
            return _pair(config)
        if args.command == "display-test":
            return _display_test(config, args.driver)
    except SideloadError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("unknown command", file=sys.stderr)
    return 2


def _daemon(config) -> int:
    daemon = build(config, connect_hardware=True)
    from sideload_pi.web import serve_in_thread

    if config.web.enabled:
        serve_in_thread(daemon)
    daemon.run()
    return 0


def _status(config) -> int:
    layout = Layout(config.root)
    live = read_live_status(layout)
    if live is not None:
        payload = live
        source = "live"
    else:
        daemon = build(config, connect_hardware=False)
        payload = daemon.status()
        source = "daemon is not running"
    if not payload["apps"]:
        print("library: empty")
    for app in payload["apps"]:
        days = "not installed" if app["days"] is None else f"{app['days']}d left"
        error = f"  error: {app['last_error']}" if app["last_error"] else ""
        print(f"{app['name']}  {app['bundle_id']}  {days}{error}")
    print(f"phase: {payload['phase']} ({source})")
    return 0


def _enable(config) -> int:
    if os.geteuid() != 0:
        raise SideloadError("Run sudo sideload-pi enable after login and the display test.")
    layout = Layout(config.root)
    problems = []
    if not config.anisette_url:
        problems.append("anisette_url is empty in the config")
    if not layout.account.exists():
        problems.append("no Apple ID is saved (sideload-pi login)")
    if config.display.version == "auto" and not layout.display_version.exists():
        problems.append("display.version is auto and display-test has not recorded a revision")
    if problems:
        raise SideloadError(
            "Not starting the daemon yet:\n- " + "\n- ".join(problems)
        )
    subprocess.run(["/bin/systemctl", "daemon-reload"], check=False)
    result = subprocess.run(
        ["/bin/systemctl", "enable", "--now", "sideload-pi.service"],
        check=False,
    )
    if result.returncode != 0:
        raise SideloadError("systemctl enable --now sideload-pi.service failed")
    print("sideload-pi.service is enabled and running.")
    return 0


def _login(config) -> int:
    daemon = build(config, connect_hardware=False)
    apple_id = input("Apple ID email: ").strip()
    password = getpass.getpass("Apple ID password (stored mode 0600 on this Pi): ")
    if not apple_id or not password:
        raise SideloadError("Apple ID and password are required")

    def code_provider(kind: str, phones=None) -> str:
        print(f"Apple wants a verification code ({kind}).", flush=True)
        phone_id = ""
        if phones and len(phones) > 1:
            print("SMS numbers:", flush=True)
            for item in phones:
                print(f"  {item.id}  {item.label}", flush=True)
            phone_id = input("Phone id: ").strip()
        elif phones:
            phone_id = phones[0].id
            print(f"SMS number {phones[0].id} ({phones[0].label}).", flush=True)
        else:
            print("Check your trusted devices. The code is not saved.", flush=True)
        entered = getpass.getpass("Code: ")
        popped = pop_code(daemon.layout)
        chosen = popped or entered
        if phone_id and ":" not in chosen:
            return f"{phone_id}:{chosen}"
        return chosen

    daemon.provisioner.login(apple_id, password, code_provider, utcnow())
    print("Signed in. The password is in", daemon.layout.account)
    return 0


def _add(config, ipa: Path) -> int:
    daemon = build(config, connect_hardware=False)
    record = daemon.library.add_ipa(
        ipa,
        max_apps=config.max_apps,
        strip_extensions=config.strip_extensions,
        now=utcnow(),
        window=timedelta(days=config.app_id_window_days),
        app_id_limit=config.max_app_ids_per_window,
    )
    print(f"added {record.name} ({record.bundle_id})")
    if record.extensions_stripped:
        print("App extensions will be removed at sign time so they do not use extra App IDs.")
    return 0


def _remove(config, bundle_id: str) -> int:
    daemon = build(config, connect_hardware=False)
    daemon.library.remove(bundle_id)
    print(f"removed {bundle_id}")
    return 0


def _refresh(config, bundle_id: str) -> int:
    layout = Layout(config.root)
    layout.ensure()
    from sideload_pi.layout import write_secret_text

    write_secret_text(layout.force, bundle_id.strip() + "\n")
    if _service_active():
        subprocess.run(["/bin/systemctl", "kill", "-s", "SIGUSR1", "sideload-pi.service"], check=False)
        print("Asked the running daemon to refresh.")
        return 0
    daemon = build(config, connect_hardware=False)
    for _ in range(config.max_apps):
        daemon.tick(utcnow())
        if daemon.phase != "success":
            break
    if daemon.phase == "error":
        print(daemon.message, file=sys.stderr)
        return 1
    if daemon.phase == "waiting_for_phone":
        print(daemon.message)
        return 1
    print(daemon.phase)
    return 0


def _display_test(config, driver: str | None) -> int:
    if _service_active():
        raise SideloadError(
            "sideload-pi.service is running and holds the panel. "
            "Stop it first: sudo systemctl stop sideload-pi"
        )
    version = driver or config.display.version
    if version == "auto":
        raise SideloadError(
            "Pass V1, V2, V3, or V4. Auto-detect is not used for the test pattern. "
            "The V1 init sequence never reads BUSY, so try one driver at a time "
            "and power-cycle the Pi between attempts."
        )
    image = render_test_pattern(version)
    try:
        panel = open_panel(
            lambda _version: LgpioIO(config),
            version,
            rotate=config.display.rotate,
            busy_timeout=20,
        )
    except Exception as exc:
        raise SideloadError(
            f"Could not init the {version} panel ({exc}). Run this on the Pi with SPI "
            "enabled. If the panel stayed busy, power-cycle before the next driver."
        ) from exc
    try:
        panel.show(image, True)
        panel.sleep()
    finally:
        panel.close()
    if version == "V1":
        note = "full refresh only, so the progress bar updates slowly on purpose"
    else:
        note = "partial refresh is available"
    print(f"Showed the {version} test pattern ({note}).")
    print("If the label is crisp, set display.version to this and restart the daemon.")
    print("If it is snow, blank, or stuck, power-cycle and try the next driver.")
    return 0


def _pair(config) -> int:
    daemon = build(config, connect_hardware=False)
    phone = daemon.devices.current()
    if phone is None:
        raise SideloadError("No iPhone is visible. Plug it into the USB port and unlock it.")
    print(daemon.devices.pair(phone.udid))
    print(f"paired {phone.name} ({phone.udid}) via {phone.via}")
    return 0


def _service_active() -> bool:
    if shutil.which("systemctl") is None:
        return False
    result = subprocess.run(
        ["systemctl", "is-active", "sideload-pi.service"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "active"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
