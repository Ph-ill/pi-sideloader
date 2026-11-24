"""On-disk layout. Secret directories are mode 0700, secret files mode 0600."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Layout:
    root: Path

    @property
    def ipas(self) -> Path:
        return self.root / "ipas"

    @property
    def secrets(self) -> Path:
        return self.root / "secrets"

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def two_fa(self) -> Path:
        return self.runtime / "2fa.code"

    @property
    def force(self) -> Path:
        return self.runtime / "force"

    @property
    def account(self) -> Path:
        return self.secrets / "apple-id.json"

    @property
    def web_token(self) -> Path:
        return self.secrets / "web.token"

    @property
    def cert_key(self) -> Path:
        return self.secrets / "development.key"

    @property
    def cert_pem(self) -> Path:
        return self.secrets / "development.crt"

    @property
    def lock_path(self) -> Path:
        return self.runtime / "daemon.lock"

    @property
    def display_version(self) -> Path:
        return self.root / "display-version"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        for directory in (self.ipas, self.secrets, self.logs, self.runtime, self.work):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)


def write_secret(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def write_secret_text(path: Path, text: str) -> None:
    write_secret(path, text.encode("utf-8"))
