from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping


class ProviderError(RuntimeError):
    pass


@dataclass(slots=True)
class JsonResponse:
    data: Any
    headers: Mapping[str, str]
    status: int


def json_request(
    url: str,
    *,
    method: str = "GET",
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    form: Mapping[str, Any] | None = None,
    timeout: int = 30,
    attempts: int = 3,
) -> JsonResponse:
    if params:
        query = urllib.parse.urlencode(
            {key: value for key, value in params.items() if value is not None}, doseq=True
        )
        url = f"{url}{'&' if '?' in url else '?'}{query}"
    body = None
    request_headers = dict(headers or {})
    if form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    last_error: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            url, data=body, headers=request_headers, method=method.upper()
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"Non-JSON response from {url}") from exc
                return JsonResponse(data=data, headers=dict(response.headers.items()), status=response.status)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            last_error = ProviderError(
                f"HTTP {exc.code} from {urllib.parse.urlsplit(url).netloc}: {raw[:300]}"
            )
            retry_after = _retry_after(exc.headers)
            if exc.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise last_error
            time.sleep(min(5.0, retry_after or (0.5 * 2**attempt)))
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(0.5 * 2**attempt)
    raise ProviderError(f"Request failed for {url}: {last_error}")


def _retry_after(headers: Mapping[str, str] | None) -> float | None:
    if not headers:
        return None
    value = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None

