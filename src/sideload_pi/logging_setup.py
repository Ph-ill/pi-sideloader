"""Log to stdout and to a file. Passwords and tokens are scrubbed."""

from __future__ import annotations

import logging
from pathlib import Path

from sideload_pi.redact import RedactFilter


def configure_logging(log_file: Path | None = None) -> None:
    root = logging.getLogger("sideload_pi")
    root.setLevel(logging.INFO)
    if root.handlers:
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.addFilter(RedactFilter())
    root.addHandler(stream)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handle = logging.FileHandler(log_file)
        handle.setFormatter(formatter)
        handle.addFilter(RedactFilter())
        root.addHandler(handle)
    root.propagate = False
