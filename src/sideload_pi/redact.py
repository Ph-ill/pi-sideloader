"""Keep passwords, tokens, and one-time codes out of logs."""

from __future__ import annotations

import logging
import re

_SECRET_QUERY = re.compile(
    r"(?i)(password|passwd|token|gs_token|security-code|x-apple-i-md|x-apple-gs-token)"
    r"([\"']?\s*[:=]\s*[\"']?)([^\s\"',}]+)"
)
_CODE = re.compile(r"\b(\d{6})\b")


def redact(text: str, *, secrets: tuple[str, ...] = ()) -> str:
    cleaned = _SECRET_QUERY.sub(r"\1\2[redacted]", text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


class RedactFilter(logging.Filter):
    def __init__(self, secrets: tuple[str, ...] = ()):
        super().__init__()
        self.secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg, secrets=self.secrets)
        if record.args:
            record.args = tuple(
                redact(arg, secrets=self.secrets) if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True


def scrub_code(text: str) -> str:
    """Used when a log line is known to be a verification prompt."""
    return _CODE.sub("[code]", text)
