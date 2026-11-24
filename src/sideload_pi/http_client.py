"""Small HTTP client so tests can substitute Apple and anisette."""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str]


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """GrandSlam answers a rejected client with a redirect to an HTML page.

    Following it turns that rejection into a plist parse error. AltSign's
    October 2026 session delegate refuses the redirect instead.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


def verified_https_context() -> ssl.SSLContext:
    """The default trust store. Hostname checks stay on; nothing is pinned by hand."""
    context = ssl.create_default_context()
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise RuntimeError("HTTPS certificate verification is not enabled")
    return context


class UrllibHttp:
    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 60,
        follow_redirects: bool = True,
    ) -> HttpResponse:
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        context = verified_https_context()
        try:
            if follow_redirects:
                response_cm = urllib.request.urlopen(req, timeout=timeout, context=context)
            else:
                opener = urllib.request.build_opener(
                    urllib.request.HTTPSHandler(context=context),
                    _RefuseRedirects,
                )
                response_cm = opener.open(req, timeout=timeout)
            with response_cm as response:
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
