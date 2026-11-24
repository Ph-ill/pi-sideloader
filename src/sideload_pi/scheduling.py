"""Expiry, retry backoff, and Apple's free-account quotas.

Apple's own account-help page (checked against the November 2026 summary of
that page) states that a free Personal Team can register up to 10 App IDs and
3 devices, each expiring after 7 days, and can install up to 3 apps per
device. Provisioning profiles for that team expire 7 days after issuance.
Re-signing an existing bundle id reuses its App ID; it does not spend another
slot. This module enforces those limits before any Apple call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sideload_pi.errors import LimitError


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    return ensure_aware(datetime.fromisoformat(text))


def format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return ensure_aware(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def whole_days_remaining(expiry: datetime | None, now: datetime) -> int | None:
    """Complete days left. Less than 24 hours but still in the future is 0."""
    if expiry is None:
        return None
    seconds = (ensure_aware(expiry) - ensure_aware(now)).total_seconds()
    if seconds <= 0:
        return 0
    return int(seconds // 86400)


def refresh_due(expiry: datetime | None, now: datetime, margin: timedelta) -> bool:
    """True when the app has never been installed or the profile is inside the margin."""
    if expiry is None:
        return True
    return ensure_aware(now) >= ensure_aware(expiry) - margin


def retry_delay_seconds(attempts: int, base: int, cap: int) -> int:
    """Exponential backoff. `attempts` is the number of failures so far, at least 1."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    delay = base
    for _ in range(attempts - 1):
        delay = min(cap, delay * 3)
    return min(cap, delay)


def retry_ready(next_retry_at: datetime | None, now: datetime) -> bool:
    if next_retry_at is None:
        return True
    return ensure_aware(now) >= ensure_aware(next_retry_at)


@dataclass(frozen=True)
class AppIdUse:
    bundle_id: str
    created_at: datetime


def uses_in_window(uses: list[AppIdUse], now: datetime, window: timedelta) -> list[AppIdUse]:
    start = ensure_aware(now) - window
    return [item for item in uses if ensure_aware(item.created_at) >= start]


def assert_can_register_app_id(
    uses: list[AppIdUse],
    bundle_id: str,
    now: datetime,
    *,
    window: timedelta,
    limit: int,
    already_registered: bool,
) -> None:
    if already_registered:
        return
    recent = uses_in_window(uses, now, window)
    if any(item.bundle_id == bundle_id for item in recent):
        return
    if len(recent) >= limit:
        raise LimitError(
            f"Free Apple ID limit: {limit} App IDs per {int(window.total_seconds() // 86400)} "
            f"days. {bundle_id} would be a new App ID and {len(recent)} were created in this "
            "window. Wait for the window to roll, or remove an app you no longer refresh. "
            "Refreshing an app that already has an App ID does not use a new slot."
        )


def assert_can_add_app(existing_bundle_ids: list[str], bundle_id: str, max_apps: int) -> None:
    if bundle_id in existing_bundle_ids:
        return
    if len(existing_bundle_ids) >= max_apps:
        raise LimitError(
            f"Free Apple ID limit: {max_apps} apps installed per device. "
            f"The library already has {len(existing_bundle_ids)}. "
            "Remove one before adding another. App extensions are stripped by default "
            "and are not counted; if you keep them, each one also counts."
        )


def assert_extension_budget(
    extension_ids: list[str],
    *,
    strip: bool,
    apps_after_add: int,
    max_apps: int,
    new_app_ids: int,
    app_ids_remaining: int,
) -> None:
    if not extension_ids:
        return
    if strip:
        return
    if apps_after_add + len(extension_ids) > max_apps:
        raise LimitError(
            "This IPA has app extensions and strip_extensions is false. "
            f"Extensions ({', '.join(extension_ids)}) each count toward the "
            f"{max_apps}-app free-account limit. Leave strip_extensions true "
            "(the default) to install the app without its extensions."
        )
    if new_app_ids > app_ids_remaining:
        raise LimitError(
            "This IPA needs more new App IDs than the free account has left "
            f"in the current 7-day window ({app_ids_remaining} remaining)."
        )
