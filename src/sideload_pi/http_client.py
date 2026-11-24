"""Small HTTP client so tests can substitute Apple and anisette."""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str]


class UrllibHttp:
    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 60,
    ) -> HttpResponse:
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return HttpResponse(
                    status=response.status,
                    body=response.read(),
                    headers={key.lower(): value for key, value in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status=exc.code,
                body=exc.read(),
                headers={key.lower(): value for key, value in exc.headers.items()},
            )
