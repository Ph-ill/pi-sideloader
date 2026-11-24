import os
from datetime import UTC, datetime, timedelta

import pytest
from tests.support import make_ipa

from sideload_pi.credentials import AccountSecrets, file_mode, save_account
from sideload_pi.errors import LimitError, SideloadError
from sideload_pi.layout import Layout
from sideload_pi.library import Library
from sideload_pi.profile import parse_mobileprovision

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def test_library_round_trip_and_secret_mode(tmp_path):
    layout = Layout(tmp_path)
    library = Library(layout)
    make_ipa(tmp_path / "notes.ipa")
    record = library.add_ipa(
        tmp_path / "notes.ipa",
        max_apps=3,
        strip_extensions=True,
        now=NOW,
        window=timedelta(days=7),
        app_id_limit=10,
    )
    assert record.bundle_id == "com.example.notes"
    assert file_mode(layout.state_path) == 0o600
    assert file_mode(layout.ipas) == 0o700
    again = Library(layout)
    again.load()
    assert again.apps["com.example.notes"].name == "Notes"
    account = AccountSecrets(apple_id="person@example.com", password="secret-value", saved_at=NOW)
    save_account(layout, account)
    assert file_mode(layout.account) == 0o600
    assert "secret-value" not in layout.state_path.read_text(encoding="utf-8")
    os.chmod(layout.account, 0o600)


def test_fairplay_and_a_fourth_app_are_refused(tmp_path):
    layout = Layout(tmp_path)
    library = Library(layout)
    make_ipa(tmp_path / "fair.ipa", bundle="com.example.store", name="Store", fairplay=True)
    with pytest.raises(SideloadError):
        library.add_ipa(
            tmp_path / "fair.ipa",
            max_apps=3,
            strip_extensions=True,
            now=NOW,
            window=timedelta(days=7),
            app_id_limit=10,
        )
    for index in range(3):
        path = tmp_path / f"{index}.ipa"
        make_ipa(path, bundle=f"com.example.app{index}", name=f"App{index}")
        library.add_ipa(
            path,
            max_apps=3,
            strip_extensions=True,
            now=NOW,
            window=timedelta(days=7),
            app_id_limit=10,
        )
    extra = tmp_path / "extra.ipa"
    make_ipa(extra, bundle="com.example.extra", name="Extra")
    with pytest.raises(LimitError):
        library.add_ipa(
            extra,
            max_apps=3,
            strip_extensions=True,
            now=NOW,
            window=timedelta(days=7),
            app_id_limit=10,
        )


def test_kept_extension_counts_apps_already_in_the_library(tmp_path):
    library = Library(Layout(tmp_path))
    for bundle, name in (("com.example.one", "One"), ("com.example.two", "Two")):
        path = tmp_path / f"{name}.ipa"
        make_ipa(path, bundle=bundle, name=name)
        library.add_ipa(
            path,
            max_apps=3,
            strip_extensions=True,
            now=NOW,
            window=timedelta(days=7),
            app_id_limit=10,
        )
    widget = tmp_path / "three.ipa"
    make_ipa(
        widget,
        bundle="com.example.three",
        name="Three",
        extensions=("com.example.three.widget",),
    )
    with pytest.raises(LimitError):
        library.add_ipa(
            widget,
            max_apps=3,
            strip_extensions=False,
            now=NOW,
            window=timedelta(days=7),
            app_id_limit=10,
        )


def test_corrupt_state_is_moved_aside_instead_of_crashing(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    layout.state_path.write_text("{", encoding="utf-8")
    library = Library(layout)
    library.load()
    assert library.apps == {}
    assert any(path.name.startswith("state.json.broken-") for path in layout.root.iterdir())


def test_profile_free_flag_and_seven_day_lifetime():
    created = NOW
    expiry = NOW + timedelta(days=7)
    blob = b"\x30\x82junk" + __import__("plistlib").dumps(
        {
            "Name": "iOS Team Provisioning Profile",
            "ExpirationDate": expiry,
            "CreationDate": created,
            "TeamIdentifier": ["TEAM"],
            "Entitlements": {"application-identifier": "TEAM.com.example.notes"},
            "ProvisionedDevices": ["UDID"],
            "IsFreeProvisioningProfile": True,
        }
    )
    info = parse_mobileprovision(blob)
    assert info.free is True
    assert info.team_id == "TEAM"
    assert info.lifetime == timedelta(days=7)
