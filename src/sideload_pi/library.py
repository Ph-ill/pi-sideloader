"""IPA library plus the schedule state stored next to it."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sideload_pi.errors import SideloadError
from sideload_pi.ipa import assert_signable, inspect_ipa
from sideload_pi.layout import Layout, write_secret_text
from sideload_pi.scheduling import (
    AppIdUse,
    assert_can_add_app,
    assert_extension_budget,
    ensure_aware,
    format_time,
    parse_time,
    uses_in_window,
)


@dataclass
class AppRecord:
    bundle_id: str
    name: str
    version: str
    ipa_name: str
    extension_ids: list[str] = field(default_factory=list)
    extensions_stripped: bool = True
    expiry: datetime | None = None
    last_success: datetime | None = None
    last_attempt: datetime | None = None
    attempts: int = 0
    last_error: str = ""
    next_retry_at: datetime | None = None
    app_id_id: str = ""

    def ipa_path(self, layout: Layout) -> Path:
        return layout.ipas / self.ipa_name


@dataclass
class Library:
    layout: Layout
    apps: dict[str, AppRecord] = field(default_factory=dict)
    app_id_uses: list[AppIdUse] = field(default_factory=list)
    account_kind: str = "unknown"
    team_id: str = ""
    last_refresh: datetime | None = None

    def load(self) -> None:
        path = self.layout.state_path
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        self.account_kind = str(data.get("account_kind") or "unknown")
        self.team_id = str(data.get("team_id") or "")
        self.last_refresh = parse_time(data.get("last_refresh"))
        self.apps = {}
        for bundle_id, raw in (data.get("apps") or {}).items():
            self.apps[bundle_id] = AppRecord(
                bundle_id=bundle_id,
                name=raw.get("name") or bundle_id,
                version=raw.get("version") or "",
                ipa_name=raw.get("ipa_name") or "",
                extension_ids=list(raw.get("extension_ids") or []),
                extensions_stripped=bool(raw.get("extensions_stripped", True)),
                expiry=parse_time(raw.get("expiry")),
                last_success=parse_time(raw.get("last_success")),
                last_attempt=parse_time(raw.get("last_attempt")),
                attempts=int(raw.get("attempts") or 0),
                last_error=str(raw.get("last_error") or ""),
                next_retry_at=parse_time(raw.get("next_retry_at")),
                app_id_id=str(raw.get("app_id_id") or ""),
            )
        self.app_id_uses = []
        for item in data.get("app_id_uses") or []:
            created = parse_time(item.get("created_at"))
            bundle = item.get("bundle_id")
            if created and bundle:
                self.app_id_uses.append(AppIdUse(str(bundle), created))

    def save(self) -> None:
        payload = {
            "account_kind": self.account_kind,
            "team_id": self.team_id,
            "last_refresh": format_time(self.last_refresh),
            "apps": {
                bundle: {
                    "name": app.name,
                    "version": app.version,
                    "ipa_name": app.ipa_name,
                    "extension_ids": app.extension_ids,
                    "extensions_stripped": app.extensions_stripped,
                    "expiry": format_time(app.expiry),
                    "last_success": format_time(app.last_success),
                    "last_attempt": format_time(app.last_attempt),
                    "attempts": app.attempts,
                    "last_error": app.last_error,
                    "next_retry_at": format_time(app.next_retry_at),
                    "app_id_id": app.app_id_id,
                }
                for bundle, app in self.apps.items()
            },
            "app_id_uses": [
                {"bundle_id": item.bundle_id, "created_at": format_time(item.created_at)}
                for item in self.app_id_uses
            ],
        }
        write_secret_text(self.layout.state_path, json.dumps(payload, indent=2) + "\n")

    def add_ipa(
        self,
        source: Path,
        *,
        max_apps: int,
        strip_extensions: bool,
        now: datetime,
        window: timedelta,
        app_id_limit: int,
    ) -> AppRecord:
        info = inspect_ipa(source)
        assert_signable(info)
        assert_can_add_app(list(self.apps), info.bundle_id, max_apps)
        remaining = app_id_limit - len(uses_in_window(self.app_id_uses, now, window))
        already = info.bundle_id in self.apps and bool(self.apps[info.bundle_id].app_id_id)
        new_ids = 0 if already else 1
        if not strip_extensions:
            new_ids += len(info.extension_ids)
        assert_extension_budget(
            info.extension_ids,
            strip=strip_extensions,
            apps_after_add=0 if info.bundle_id in self.apps else 1,
            max_apps=max_apps,
            new_app_ids=new_ids,
            app_ids_remaining=max(0, remaining),
        )
        self.layout.ensure()
        dest = self.layout.ipas / info.safe_filename
        if source.resolve() != dest.resolve():
            shutil.copy2(source, dest)
        previous = self.apps.get(info.bundle_id)
        record = AppRecord(
            bundle_id=info.bundle_id,
            name=info.name,
            version=info.version,
            ipa_name=info.safe_filename,
            extension_ids=list(info.extension_ids),
            extensions_stripped=bool(strip_extensions and info.extension_ids),
            expiry=previous.expiry if previous else None,
            last_success=previous.last_success if previous else None,
            last_attempt=previous.last_attempt if previous else None,
            attempts=0,
            last_error="",
            next_retry_at=None,
            app_id_id=previous.app_id_id if previous else "",
        )
        self.apps[info.bundle_id] = record
        self.save()
        return record

    def remove(self, bundle_id: str) -> None:
        record = self.apps.pop(bundle_id, None)
        if record is None:
            raise SideloadError(f"no app {bundle_id} in the library")
        path = self.layout.ipas / record.ipa_name
        if path.exists():
            path.unlink()
        self.save()

    def note_app_id(self, bundle_id: str, app_id_id: str, created: bool, now: datetime) -> None:
        record = self.apps[bundle_id]
        record.app_id_id = app_id_id
        if created:
            self.app_id_uses.append(AppIdUse(bundle_id, ensure_aware(now)))
        self.save()

    def mark_success(self, bundle_id: str, expiry: datetime, now: datetime) -> None:
        record = self.apps[bundle_id]
        record.expiry = ensure_aware(expiry)
        record.last_success = ensure_aware(now)
        record.last_attempt = ensure_aware(now)
        record.attempts = 0
        record.last_error = ""
        record.next_retry_at = None
        self.last_refresh = ensure_aware(now)
        self.save()

    def mark_failure(self, bundle_id: str, message: str, now: datetime, *, delay: timedelta) -> None:
        record = self.apps[bundle_id]
        record.last_attempt = ensure_aware(now)
        record.attempts += 1
        record.last_error = message
        record.next_retry_at = ensure_aware(now) + delay
        self.save()
