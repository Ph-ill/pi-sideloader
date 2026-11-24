from datetime import UTC, datetime, timedelta

import pytest

from sideload_pi.errors import LimitError
from sideload_pi.scheduling import (
    AppIdUse,
    assert_can_add_app,
    assert_can_register_app_id,
    assert_extension_budget,
    ntp_synchronized,
    refresh_due,
    retry_delay_seconds,
    whole_days_remaining,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def test_days_remaining_floors_partial_days():
    assert whole_days_remaining(None, NOW) is None
    assert whole_days_remaining(NOW - timedelta(hours=1), NOW) == 0
    assert whole_days_remaining(NOW + timedelta(hours=23), NOW) == 0
    assert whole_days_remaining(NOW + timedelta(hours=36), NOW) == 1


def test_refresh_is_due_inside_the_margin_and_when_never_installed():
    margin = timedelta(days=1)
    assert refresh_due(None, NOW, margin) is True
    assert refresh_due(NOW + timedelta(days=6), NOW, margin) is False
    assert refresh_due(NOW + timedelta(hours=12), NOW, margin) is True


def test_retry_backoff_triples_and_caps_at_six_hours():
    assert retry_delay_seconds(1, 300, 21600) == 300
    assert retry_delay_seconds(2, 300, 21600) == 900
    assert retry_delay_seconds(3, 300, 21600) == 2700
    assert retry_delay_seconds(4, 300, 21600) == 8100
    assert retry_delay_seconds(5, 300, 21600) == 21600
    with pytest.raises(ValueError):
        retry_delay_seconds(0, 300, 21600)


def test_reusing_a_bundle_does_not_spend_an_app_id():
    uses = [AppIdUse(f"com.example.{i}", NOW - timedelta(days=1)) for i in range(10)]
    assert_can_register_app_id(
        uses,
        "com.example.0",
        NOW,
        window=timedelta(days=7),
        limit=10,
        already_registered=True,
    )
    with pytest.raises(LimitError):
        assert_can_register_app_id(
            uses,
            "com.example.new",
            NOW,
            window=timedelta(days=7),
            limit=10,
            already_registered=False,
        )


def test_recreating_a_bundle_already_in_the_window_still_counts():
    uses = [AppIdUse(f"com.example.{i}", NOW - timedelta(days=1)) for i in range(9)]
    uses.append(AppIdUse("com.example.notes", NOW - timedelta(days=1)))
    with pytest.raises(LimitError):
        assert_can_register_app_id(
            uses,
            "com.example.notes",
            NOW,
            window=timedelta(days=7),
            limit=10,
            already_registered=False,
        )


def test_missing_timedatectl_does_not_claim_the_clock_is_unsynced(monkeypatch):
    monkeypatch.setattr("sideload_pi.scheduling.shutil.which", lambda _name: None)
    assert ntp_synchronized() is None


def test_timedatectl_no_is_not_synced(monkeypatch):
    monkeypatch.setattr("sideload_pi.scheduling.shutil.which", lambda _name: "/usr/bin/timedatectl")

    class _Result:
        returncode = 0
        stdout = "no\n"

    monkeypatch.setattr(
        "sideload_pi.scheduling.subprocess.run",
        lambda *_args, **_kwargs: _Result(),
    )
    assert ntp_synchronized() is False


def test_timedatectl_failure_is_not_synced(monkeypatch):
    import subprocess

    monkeypatch.setattr("sideload_pi.scheduling.shutil.which", lambda _name: "/usr/bin/timedatectl")

    class _Result:
        def __init__(self, returncode, stdout):
            self.returncode = returncode
            self.stdout = stdout

    monkeypatch.setattr(
        "sideload_pi.scheduling.subprocess.run",
        lambda *_args, **_kwargs: _Result(1, ""),
    )
    assert ntp_synchronized() is False
    monkeypatch.setattr(
        "sideload_pi.scheduling.subprocess.run",
        lambda *_args, **_kwargs: _Result(0, "maybe\n"),
    )
    assert ntp_synchronized() is False

    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="timedatectl", timeout=5)

    monkeypatch.setattr("sideload_pi.scheduling.subprocess.run", _timeout)
    assert ntp_synchronized() is False


def test_fourth_app_is_refused_and_extensions_count_when_kept():
    existing = ["a.b.c", "d.e.f", "g.h.i"]
    assert_can_add_app(existing, "a.b.c", 3)
    with pytest.raises(LimitError):
        assert_can_add_app(existing, "j.k.l", 3)
    assert_extension_budget(
        ["a.b.c.widget"],
        strip=True,
        apps_after_add=1,
        max_apps=3,
        new_app_ids=1,
        app_ids_remaining=0,
    )
    # Two apps already in the library, plus the one being added, plus one
    # extension, is 4 and does not fit in 3. Passing only "1" for the new app
    # would hide that.
    with pytest.raises(LimitError):
        assert_extension_budget(
            ["com.example.notes.widget"],
            strip=False,
            apps_after_add=3,
            max_apps=3,
            new_app_ids=2,
            app_ids_remaining=10,
        )
    assert_extension_budget(
        ["com.example.notes.widget"],
        strip=False,
        apps_after_add=1,
        max_apps=3,
        new_app_ids=2,
        app_ids_remaining=10,
    )
