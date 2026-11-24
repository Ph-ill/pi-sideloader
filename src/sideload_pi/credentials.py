"""Apple ID material. The password is stored so unattended refresh can re-login.

The file is mode 0600 inside a 0700 directory. Anyone who can read it can
sign in as this Apple ID. Use a dedicated Apple ID. Raspberry Pi OS does not
encrypt the SD card unless you set that up yourself.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sideload_pi.layout import Layout, write_secret_text
from sideload_pi.scheduling import format_time, parse_time


@dataclass
class AccountSecrets:
    apple_id: str
    password: str
    adsid: str = ""
    gs_token: str = ""
    token_expiry: datetime | None = None
    saved_at: datetime | None = None

    @property
    def has_session(self) -> bool:
        return bool(self.adsid and self.gs_token)


def load_account(layout: Layout) -> AccountSecrets | None:
    path = layout.account
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return AccountSecrets(
        apple_id=str(data.get("apple_id") or ""),
        password=str(data.get("password") or ""),
        adsid=str(data.get("adsid") or ""),
        gs_token=str(data.get("gs_token") or ""),
        token_expiry=parse_time(data.get("token_expiry")),
        saved_at=parse_time(data.get("saved_at")),
    )


def save_account(layout: Layout, account: AccountSecrets) -> None:
    if not account.apple_id or not account.password:
        raise ValueError("apple id and password are required")
    payload = {
        "apple_id": account.apple_id,
        "password": account.password,
        "adsid": account.adsid,
        "gs_token": account.gs_token,
        "token_expiry": format_time(account.token_expiry),
        "saved_at": format_time(account.saved_at),
    }
    write_secret_text(layout.account, json.dumps(payload, indent=2) + "\n")


def load_web_token(layout: Layout) -> str:
    path = layout.web_token
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def save_web_token(layout: Layout, token: str) -> None:
    write_secret_text(layout.web_token, token + "\n")


def file_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
