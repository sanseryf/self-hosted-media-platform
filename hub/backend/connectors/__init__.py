"""L2 connector gateway package.

Public surface mirrors db.py's module-singleton idiom so app.py wires it the same
way (init on startup, get() from routers, close on shutdown):

    await connectors.init_gateway()      # in lifespan startup
    gw = connectors.get_gateway()        # in a router
    await connectors.close_gateway()     # in lifespan shutdown

build_gateway() reads the same env vars the existing hub modules already use
(JELLYFIN_URL/…_API_KEY, RADARR_*, SONARR_*, JELLYSEERR_*), defaulting host to
SRV like watch_poll.py / unwatched-cleanup.py do. A connector with a missing key
is constructed *disabled* — it answers health='not configured' and every read
returns None, and it never makes a network call (so a dev box can't accidentally
reach the production defaults).
"""
from __future__ import annotations

import os

from .base import (
    ConnectorAuthError,
    ConnectorBadResponse,
    ConnectorError,
    ConnectorNotFound,
    ConnectorUnavailable,
)
from .gateway import Gateway
from .jellyfin import JellyfinConnector
from .jellyseerr import JellyseerrConnector
from .models import HealthStatus, MediaRef, SystemState
from .radarr import RadarrConnector
from .sonarr import SonarrConnector

__all__ = [
    "Gateway", "build_gateway", "init_gateway", "get_gateway", "close_gateway",
    "MediaRef", "SystemState", "HealthStatus",
    "ConnectorError", "ConnectorAuthError", "ConnectorNotFound",
    "ConnectorUnavailable", "ConnectorBadResponse",
]


def _env(*names, default=""):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def build_gateway() -> Gateway:
    srv = _env("SRV", default="10.0.0.10")
    return Gateway(
        jellyfin=JellyfinConnector(
            _env("JELLYFIN_URL", default=f"http://{srv}:8096"),
            _env("JELLYFIN_API_KEY", "JELLYFIN"),
        ),
        radarr=RadarrConnector(
            _env("RADARR_URL", default=f"http://{srv}:7878"),
            _env("RADARR_API_KEY", "RADARR"),
        ),
        sonarr=SonarrConnector(
            _env("SONARR_URL", default=f"http://{srv}:8989"),
            _env("SONARR_API_KEY", "SONARR"),
        ),
        jellyseerr=JellyseerrConnector(
            _env("JELLYSEERR_URL", default=f"http://{srv}:5055"),
            _env("JELLYSEERR_API_KEY", "JELLYSEERR"),
        ),
    )


_gateway: Gateway | None = None


def get_gateway() -> Gateway | None:
    return _gateway


async def init_gateway() -> None:
    global _gateway
    if _gateway is None:
        _gateway = build_gateway()


async def close_gateway() -> None:
    global _gateway
    if _gateway is not None:
        await _gateway.aclose()
        _gateway = None
