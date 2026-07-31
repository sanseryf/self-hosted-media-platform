"""L2 gateway observability endpoints (session-gated, like the rest of /api/*).

    GET /api/gateway/health           per-connector up/down + latency
    GET /api/gateway/locate?tmdb=…    each system's view of one title (the L3 join, demoable now)

Thin HTTP skin over connectors.Gateway. Degrades like every other hub router:
if the gateway isn't initialized (shouldn't happen post-startup) it returns
ok:false rather than 500.
"""
from __future__ import annotations

from fastapi import APIRouter

import connectors

router = APIRouter()


@router.get("/health")
async def health():
    gw = connectors.get_gateway()
    if gw is None:
        return {"ok": False, "connectors": []}
    statuses = await gw.health()
    return {
        "ok": all(s.ok for s in statuses),
        "connectors": [s.model_dump() for s in statuses],
    }


@router.get("/locate")
async def locate(tmdb: str | None = None, imdb: str | None = None,
                 tvdb: str | None = None, media_type: str | None = None):
    """Every system's view of one title, keyed by whatever external ids you pass.
    This is exactly the fan-out L3's lifecycle tracker will consume — surfaced now
    so the gateway is verifiable end-to-end the moment real keys are in place."""
    gw = connectors.get_gateway()
    if gw is None:
        return {"ok": False, "views": {}}
    if not any((tmdb, imdb, tvdb)):
        return {"ok": False, "error": "pass at least one of tmdb/imdb/tvdb", "views": {}}
    views = await gw.locate(tmdb=tmdb, imdb=imdb, tvdb=tvdb, media_type=media_type)
    return {"ok": True, "views": {
        k: (v.model_dump() if v is not None else None) for k, v in views.items()
    }}
