"""
Shared outbound-HTTP resilience for the GitHub and Devin clients.

- `client()` builds an `httpx.Client` with explicit connect/read/write/pool
  timeouts (HTTP_CONNECT_TIMEOUT / HTTP_READ_TIMEOUT).
- `request()` sends one request and retries transient failures with
  exponential backoff plus full jitter: connect/read/pool timeouts, transport
  errors, 5xx responses, and 429 / 503 honouring `Retry-After`.

Only idempotent methods (GET, HEAD, OPTIONS, PUT, DELETE) are retried on
timeouts / transport errors / 5xx by default, because a timed-out POST may
already have been applied server-side. Non-idempotent calls (create_issue,
create_session, post_comment, ...) are retried only on 429, which by
definition means the server did not process the request. Callers can opt a
POST into full retries with `idempotent=True` (e.g. once an idempotency key
is supported).
"""

import logging
import os
import random
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime

import httpx

from app.observability import EXTERNAL_RETRIES, span

log = logging.getLogger(__name__)

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=_float_env("HTTP_CONNECT_TIMEOUT", 5.0),
        read=_float_env("HTTP_READ_TIMEOUT", 30.0),
        write=_float_env("HTTP_READ_TIMEOUT", 30.0),
        pool=_float_env("HTTP_CONNECT_TIMEOUT", 5.0),
    )


def client(**kwargs) -> httpx.Client:
    """`httpx.Client` with the service-wide timeouts applied."""
    kwargs.setdefault("timeout", timeout())
    return httpx.Client(**kwargs)


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse `Retry-After` (delta-seconds or HTTP-date); None if absent/invalid."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, when.timestamp() - time.time())


def backoff_seconds(
    attempt: int, base: float, cap: float, rand: Callable[[], float] = random.random
) -> float:
    """Full-jitter exponential backoff: uniform(0, min(cap, base * 2**attempt))."""
    return rand() * min(cap, base * (2**attempt))


def request(
    http: httpx.Client,
    method: str,
    url: str,
    *,
    idempotent: bool | None = None,
    max_attempts: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs,
) -> httpx.Response:
    """Send a request; retry transient failures when the call is idempotent.

    Returns the final response (raising `httpx.HTTPStatusError` for a final
    4xx/5xx via `raise_for_status`), or re-raises the final transport error.
    """
    method = method.upper()
    if idempotent is None:
        idempotent = method in IDEMPOTENT_METHODS
    attempts = max(
        1, max_attempts if max_attempts is not None else _int_env("HTTP_MAX_ATTEMPTS", 4)
    )
    base = max(0.0, _float_env("HTTP_BACKOFF_BASE_SECONDS", 0.5))
    cap = max(0.0, _float_env("HTTP_BACKOFF_CAP_SECONDS", 20.0))

    host = httpx.URL(url).host
    for attempt in range(attempts):
        last = attempt == attempts - 1
        try:
            with span(
                f"http {method}",
                **{"http.method": method, "http.url": url, "http.retry_attempt": attempt},
            ):
                response = http.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if last or not idempotent:
                raise
            EXTERNAL_RETRIES.labels(host=host).inc()
            delay = backoff_seconds(attempt, base, cap)
            log.warning(
                "%s %s failed (%s); retry %d/%d in %.2fs",
                method,
                url,
                type(exc).__name__,
                attempt + 1,
                attempts - 1,
                delay,
            )
            sleep(delay)
            continue
        retryable = response.status_code == 429 or (
            idempotent and response.status_code in RETRY_STATUS
        )
        if not retryable or last:
            response.raise_for_status()
            return response
        EXTERNAL_RETRIES.labels(host=host).inc()
        retry_after = retry_after_seconds(response)
        delay = min(
            retry_after if retry_after is not None else backoff_seconds(attempt, base, cap), cap
        )
        log.warning(
            "%s %s returned %d; retry %d/%d in %.2fs",
            method,
            url,
            response.status_code,
            attempt + 1,
            attempts - 1,
            delay,
        )
        sleep(delay)
    raise AssertionError("unreachable")
