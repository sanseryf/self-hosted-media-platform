"""Resilient HTTP base for the L2 connector gateway.

Every upstream (Jellyfin, Sonarr, Radarr, Jellyseerr) speaks HTTP+JSON but each
one fails differently, so the *unhappy paths* — not the happy ones — are what
this base standardizes. One `_request` gives every connector:

  • timeouts (connect + read) so a hung upstream can't wedge the event loop;
  • bounded retry with exponential backoff + jitter on *transient* faults only
    (connection errors, read timeouts, 5xx) — never on a 4xx, which retrying
    won't fix;
  • a typed error taxonomy the callers can branch on:
      ConnectorAuthError     – 401/403, raised immediately, no retry
      ConnectorNotFound      – 404, so find_*() can quietly return None
      ConnectorUnavailable   – connection/timeout/5xx after retries exhausted
      ConnectorBadResponse   – 2xx but the body wasn't the JSON we expected
      ConnectorError         – base / other 4xx
  • a "disabled" state: a connector with no URL or API key never makes a network
    call at all (find_*→None, health→not-configured). This is deliberate — the
    URL defaults point at PRODUCTION, so gating on the key being present is what
    keeps a dev box from silently poking the live server (same lesson as
    HUB_JELLYFIN_LOGIN_ENABLED in auth.py).

GET-only. Every read here is idempotent, so blind retry is safe; if L3's policy
engine later adds POSTs, they must NOT reuse this retry path unthinkingly.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from .models import HealthStatus

log = logging.getLogger("hub.connectors")


class ConnectorError(RuntimeError):
    """Base for all connector faults (also covers unexpected 4xx)."""


class ConnectorAuthError(ConnectorError):
    """401/403 — bad or missing API key. Never retried."""


class ConnectorNotFound(ConnectorError):
    """404 — the resource doesn't exist. find_*() maps this to None."""


class ConnectorUnavailable(ConnectorError):
    """Upstream unreachable / timed out / 5xx after retries were exhausted."""


class ConnectorBadResponse(ConnectorError):
    """2xx but the body wasn't parseable JSON of the shape we expected."""


class HttpConnector:
    name: str = "connector"
    api_base: str = ""          # e.g. "/api/v3" for the *arr apps, "" for Jellyfin
    _ping_path: str = "/"       # a cheap authenticated endpoint for health()

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 15.0,
        connect_timeout: float = 5.0,
        retries: int = 2,
        backoff_base: float = 0.4,
        transport: httpx.BaseTransport | None = None,  # injected by tests (MockTransport)
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.retries = retries
        self.backoff_base = backoff_base
        # No URL or no key → disabled: we will never make a call. See class docstring.
        self.enabled = bool(self.base_url and self.api_key)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            transport=transport,
        )

    # ── subclasses override these two ─────────────────────────────────────────
    def _auth_headers(self) -> dict[str, str]:
        return {}

    # ── core ──────────────────────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        return f"{self.base_url}{self.api_base}{path}"

    def _backoff(self, attempt: int) -> float:
        # exp backoff + full jitter; backoff_base=0 (tests) collapses to no sleep
        return self.backoff_base * (2 ** attempt) + random.uniform(0, self.backoff_base)

    async def _request(self, method: str, path: str, *, params: dict | None = None) -> httpx.Response:
        if not self.enabled:
            raise ConnectorUnavailable(f"{self.name} is not configured (missing URL or API key)")
        url = self._url(path)
        headers = self._auth_headers()
        last: ConnectorError | None = None
        for attempt in range(self.retries + 1):
            try:
                r = await self._client.request(method, url, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = ConnectorUnavailable(f"{self.name} unreachable: {e!s}")
            else:
                sc = r.status_code
                if sc in (401, 403):
                    raise ConnectorAuthError(f"{self.name} auth failed (HTTP {sc})")
                if sc == 404:
                    raise ConnectorNotFound(f"{self.name} 404 for {path}")
                if sc >= 500:
                    last = ConnectorUnavailable(f"{self.name} upstream error (HTTP {sc})")
                elif sc >= 400:
                    # Other 4xx (400/422/…) — a client-side problem; retrying won't help.
                    raise ConnectorError(f"{self.name} HTTP {sc}: {r.text[:200]}")
                else:
                    return r
            # transient (timeout/transport/5xx): back off and retry if attempts remain
            if attempt < self.retries:
                await asyncio.sleep(self._backoff(attempt))
        assert last is not None
        log.warning("%s: giving up after %d attempts: %s", self.name, self.retries + 1, last)
        raise last

    async def _get_json(self, path: str, params: dict | None = None):
        r = await self._request("GET", path, params=params)
        try:
            return r.json()
        except ValueError as e:
            raise ConnectorBadResponse(f"{self.name}: non-JSON response from {path}: {e!s}") from e

    async def _post_once(self, path: str, json_body: dict | None = None, params: dict | None = None):
        """Single-attempt POST — NO retry, deliberately. Writes (approve a request,
        etc.) are not safely idempotent under blind retry, so this never reuses the
        GET retry path. Same typed-error taxonomy; caller decides how to react."""
        if not self.enabled:
            raise ConnectorUnavailable(f"{self.name} is not configured")
        try:
            r = await self._client.post(self._url(path), headers=self._auth_headers(),
                                        json=json_body, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise ConnectorUnavailable(f"{self.name} unreachable: {e!s}") from e
        sc = r.status_code
        if sc in (401, 403):
            raise ConnectorAuthError(f"{self.name} auth failed (HTTP {sc})")
        if sc == 404:
            raise ConnectorNotFound(f"{self.name} 404 for {path}")
        if sc >= 400:
            raise ConnectorError(f"{self.name} HTTP {sc}: {r.text[:200]}")
        try:
            return r.json() if r.content else {}
        except ValueError:
            return {}

    async def health(self) -> HealthStatus:
        if not self.enabled:
            return HealthStatus(source=self.name, ok=False, detail="not configured")
        t = time.perf_counter()
        try:
            await self._get_json(self._ping_path)
        except ConnectorError as e:
            return HealthStatus(source=self.name, ok=False, detail=str(e))
        return HealthStatus(source=self.name, ok=True, latency_ms=int((time.perf_counter() - t) * 1000))

    async def aclose(self) -> None:
        await self._client.aclose()
