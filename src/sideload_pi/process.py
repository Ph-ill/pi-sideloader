"""Run a command and hand partial output to a callback as it arrives."""

from __future__ import annotations

import os
import selectors
import subprocess
import time


class CommandError(Exception):
    def __init__(self, argv: list[str], returncode: int, output: str):
        self.argv = argv
        self.returncode = returncode
        self.output = output
        shown = argv[0] if argv else "command"
        if "timed out" in output:
            super().__init__(f"{shown} timed out")
        else:
            super().__init__(f"{shown} exited {returncode}")


def run_command(argv: list[str], on_text=None, timeout: float | None = None) -> str:
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)
    selector = selectors.DefaultSelector()
    selector.register(fd, selectors.EVENT_READ)
    chunks: list[bytes] = []
    started = time.monotonic()
    try:
        while True:
            if timeout is not None and time.monotonic() - started > timeout:
                proc.kill()
                proc.wait(timeout=5)
                raise CommandError(argv, -1, _decode(chunks) + "\n(timed out)")
            ready = selector.select(timeout=0.2)
            if ready:
                data = proc.stdout.read()
                if data:
                    chunks.append(data)
                    if on_text and (b"\n" in data or b"\r" in data):
                        on_text(_decode(chunks))
                elif proc.poll() is not None:
                    break
            elif proc.poll() is not None:
                rest = proc.stdout.read() or b""
                if rest:
                    chunks.append(rest)
                break
        returncode = proc.wait(timeout=5)
    finally:
        selector.close()
    output = _decode(chunks)
    if on_text and output:
        on_text(output)
    if returncode != 0:
        raise CommandError(argv, returncode, output)
    return output


def _decode(chunks: list[bytes]) -> str:
    return b"".join(chunks).decode("utf-8", errors="replace")
