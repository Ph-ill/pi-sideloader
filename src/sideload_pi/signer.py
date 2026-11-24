"""Re-sign an IPA with zsign.

``-2`` asks for a SHA-256-only code directory. Dual SHA-1 + SHA-256
directories are rejected by AMFI on iOS 17 and later (the failure reported
against AltServer-Linux 0.0.5, fixed there by switching the signer to zsign).
That failure was not reproduced on a device in this project. ``sha256_only``
defaults on so a fresh install does not hit it.

``-E`` and ``-W`` drop app extensions and a watch app. A free account needs a
separate App ID for each of those, and they count toward the 3-app and
10-App-ID limits. The default is to strip them and say so.
"""

from __future__ import annotations

from pathlib import Path

from sideload_pi.errors import SignError
from sideload_pi.process import CommandError


class Signer:
    def __init__(self, config, runner):
        self.config = config
        self.runner = runner

    def sign(
        self,
        ipa: Path,
        *,
        key: Path,
        certificate: Path,
        profile: Path,
        output: Path,
        strip_extensions: bool,
        strip_watch: bool,
    ) -> None:
        argv = [
            self.config.zsign_bin,
            "-k",
            str(key),
            "-c",
            str(certificate),
            "-m",
            str(profile),
            "-o",
            str(output),
            "-z",
            "9",
            "-f",
        ]
        if self.config.sha256_only:
            argv.append("-2")
        if strip_extensions:
            argv.append("-E")
        if strip_watch:
            argv.append("-W")
        argv.append(str(ipa))
        try:
            self.runner(argv, timeout=900)
        except CommandError as exc:
            tail = exc.output.strip().splitlines()
            detail = tail[-1] if tail else f"exit {exc.returncode}"
            raise SignError(f"zsign failed: {detail}") from exc
        except OSError as exc:
            raise SignError(
                "zsign is not installed. The install script builds it from source."
            ) from exc
