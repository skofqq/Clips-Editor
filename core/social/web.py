"""Small JSON-over-HTTPS helper shared by the TikTok and Instagram clients."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from core.social.errors import SocialError

ErrorParser = Callable[[int, dict], SocialError]


def _default_error(status: int, payload: dict) -> SocialError:
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return SocialError(str(error.get("message") or error.get("code") or f"HTTP {status}"), status,
                           str(error.get("code", "")))
    return SocialError(f"HTTP {status}: {error or payload}", status)


def call(method: str, url: str, *, json_body: Any = None, form: dict | None = None, data: bytes | None = None,
         headers: dict | None = None, timeout: float = 60, parse_error: ErrorParser = _default_error) -> dict:
    """Send a request and return the JSON reply (``{}`` when empty)."""
    body = data
    all_headers = dict(headers or {})
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        all_headers.setdefault("Content-Type", "application/json; charset=UTF-8")
    elif form is not None:
        body = urllib.parse.urlencode(form).encode()
        all_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    request = urllib.request.Request(url, data=body, method=method)
    for key, value in all_headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {"error": raw.decode("utf-8", "replace")[:300]}
        raise parse_error(exc.code, payload if isinstance(payload, dict) else {}) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SocialError(f"No connection: {exc}") from exc
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise SocialError(f"Unexpected reply: {raw[:200]!r}") from exc
    return payload if isinstance(payload, dict) else {"data": payload}
