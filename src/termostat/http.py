"""Minimal HTTP helpers on top of urllib, with gzip support."""

import gzip
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import Message
from typing import Any

DEFAULT_TIMEOUT = 30.0


@dataclass
class Response:
    status: int
    headers: Message
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


def get(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    digest_auth: tuple[str, str] | None = None,
    context: ssl.SSLContext | None = None,
) -> Response:
    """GET a URL. Non-2xx responses (including 304) are returned, not raised.

    Network failures raise OSError (urllib.error.URLError, TimeoutError, ssl.SSLError). digest_auth
    is a (username, password) pair for HTTP digest authentication. context overrides the TLS
    settings, e.g. to trust a private CA.
    """
    return request("GET", url, headers, timeout=timeout, digest_auth=digest_auth, context=context)


def request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
    digest_auth: tuple[str, str] | None = None,
    context: ssl.SSLContext | None = None,
) -> Response:
    """Send a request, with body (if not None) encoded as JSON. Errors are handled as in get()."""
    headers = {"Accept-Encoding": "gzip", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    handlers: list[urllib.request.BaseHandler] = []
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    if digest_auth is not None:
        passwords = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        passwords.add_password(None, url, *digest_auth)
        handlers.append(urllib.request.HTTPDigestAuthHandler(passwords))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=timeout) as response:
            status, response_headers, body = response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        status, response_headers, body = error.code, error.headers, error.read()

    if response_headers.get("Content-Encoding", "").lower() == "gzip" and body:
        body = gzip.decompress(body)
    return Response(status, response_headers, body)
