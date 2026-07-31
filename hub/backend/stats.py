"""Read-only stats endpoints backing the landing-page dashboard."""
import asyncio
import json
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter

import db

router = APIRouter()

# Filesystem that holds the media library, as mounted inside the hub container.
_MEDIA_PATH = os.environ.get("MEDIA_BASE", "/data/media")

# The public "door" services, reached on the LAN for a live/down check. Canonical
# list lives in config/services.json (also read by devops-console/server.py via
# its /srv/media mount) so the 4 doors are defined in exactly one place.
_STATUS_HOST = os.environ.get("STATUS_HOST", "10.0.0.10")
_SERVICES_CONFIG_PATH = Path(__file__).parent / "config" / "services.json"


def _load_services_config():
    with open(_SERVICES_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)["services"]


_SERVICES_RAW = _load_services_config()
# (key, name, health_url) tuples, same shape the rest of this module already expects.
_SERVICES = [
    (s["key"], s["name"], f"http://{_STATUS_HOST}:{s['port']}{s['health_path']}")
    for s in _SERVICES_RAW
]

_STATUS_SCHEMA = """
CREATE TABLE IF NOT EXISTS status_history (
    id BIGSERIAL PRIMARY KEY,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    up INT NOT NULL, total INT NOT NULL, overall TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_status_time ON status_history(checked_at);
"""

_SERVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS service_history (
    id BIGSERIAL PRIMARY KEY,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    service_key TEXT NOT NULL, up BOOLEAN NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_svc_time ON service_history(service_key, checked_at);
"""


def _ping(url):
    """Alive if the service answers at all — even an HTTP error means it's up. Also
    timed, so devops-console can source its latency sparkline from here instead of
    probing the doors itself (Stage 6 - hub is the single source of truth)."""
    t = time.time()
    try:
        with urllib.request.urlopen(url, timeout=2.5):
            return True, int((time.time() - t) * 1000)
    except urllib.error.HTTPError:
        return True, int((time.time() - t) * 1000)
    except Exception:
        return False, None


async def _check():
    results = await asyncio.gather(*[asyncio.to_thread(_ping, u) for _, _, u in _SERVICES])
    svc = [{"key": k, "name": n, "up": bool(up), "latency_ms": ms}
           for (k, n, _), (up, ms) in zip(_SERVICES, results)]
    up = sum(1 for s in svc if s["up"])
    overall = "live" if up == len(svc) else ("down" if up == 0 else "partial")
    return svc, up, len(svc), overall


_HOST_SPEC_CONFIG_PATH = Path(__file__).parent / "config" / "host_spec.json"


@router.get("/host-spec")
async def host_spec():
    """Hand-maintained hardware spec for the 'Under the hood' panel, served from
    config so the landing page has no hardcoded hardware text to fall out of date."""
    try:
        with open(_HOST_SPEC_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {"ok": True, "rows": data["rows"], "roadmap": data["roadmap"]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "rows": [], "roadmap": ""}


@router.get("/services")
async def services():
    """Door metadata (name/desc/icon/link) for the landing page nav — config-driven
    so it can't drift from the status pinger's own list."""
    return {"ok": True, "services": [
        {"key": s["key"], "name": s["name"], "desc": s["desc"],
         "icon": s["icon"], "url": s["public_url"]}
        for s in _SERVICES_RAW
    ]}


@router.get("/status")
async def status():
    svc, up, total, overall = await _check()
    return {"ok": True, "services": svc, "up": up, "total": total, "overall": overall}


@router.get("/status/timeline")
async def status_timeline(days: int = 30):
    """Per-service, per-day uptime %% — the Statuspage-style bar board."""
    days = max(1, min(days, 90))
    p = db.pool()
    if p is None:
        return {"ok": False, "services": []}
    try:
        async with p.acquire() as c:
            rows = await c.fetch(
                """
                SELECT service_key,
                       date_trunc('day', checked_at)::date AS d,
                       round(100.0 * sum(CASE WHEN up THEN 1 ELSE 0 END) / count(*), 2) AS pct
                FROM service_history
                WHERE checked_at >= now() - make_interval(days => $1)
                GROUP BY service_key, d ORDER BY d
                """, days)
    except Exception:  # noqa: BLE001
        return {"ok": False, "services": []}
    names = {k: n for k, n, _ in _SERVICES}
    bykey = {}
    for r in rows:
        bykey.setdefault(r["service_key"], {})[r["d"].isoformat()] = float(r["pct"])
    services = []
    for k, n, _ in _SERVICES:
        dd = bykey.get(k, {})
        vals = list(dd.values())
        up = round(sum(vals) / len(vals), 2) if vals else None
        services.append({"key": k, "name": names[k], "uptime_pct": up,
                         "days": [{"date": dt, "pct": pv} for dt, pv in sorted(dd.items())]})
    return {"ok": True, "days": days, "services": services}


async def status_sampler(interval: int = 60):
    """Background task: record service status into status_history every `interval` s."""
    while True:  # wait for the DB, then create the table once
        p = db.pool()
        if p is not None:
            try:
                async with p.acquire() as c:
                    await c.execute(_STATUS_SCHEMA)
                    await c.execute(_SERVICE_SCHEMA)
                break
            except Exception:
                pass
        await asyncio.sleep(5)
    while True:
        try:
            svc, up, total, overall = await _check()
            p = db.pool()
            if p is not None:
                async with p.acquire() as c:
                    await c.execute("INSERT INTO status_history(up,total,overall) VALUES($1,$2,$3)",
                                    up, total, overall)
                    await c.executemany("INSERT INTO service_history(service_key,up) VALUES($1,$2)",
                                        [(x["key"], x["up"]) for x in svc])
                    await c.execute("DELETE FROM status_history WHERE checked_at < now() - interval '35 days'")
                    await c.execute("DELETE FROM service_history WHERE checked_at < now() - interval '95 days'")
        except Exception:
            pass
        await asyncio.sleep(interval)


@router.get("/storage")
async def storage():
    """Live disk usage of the media library filesystem (for the landing bar)."""
    try:
        total, used, free = shutil.disk_usage(_MEDIA_PATH)
        return {"ok": True, "total": total, "used": used, "free": free,
                "pct": round(100 * used / total, 1) if total else 0}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.get("/summary")
async def summary():
    p = db.pool()
    if p is None:
        return {"ok": False, "types": [], "totals": {"titles": 0, "bytes": 0}}
    async with p.acquire() as c:
        rows = await c.fetch(
            """
            SELECT s.media_type, s.title_count, s.total_bytes
            FROM library_snapshots s
            JOIN (SELECT media_type, max(taken_at) mx
                  FROM library_snapshots GROUP BY media_type) l
              ON s.media_type = l.media_type AND s.taken_at = l.mx
            ORDER BY s.media_type
            """
        )
    types = [dict(r) for r in rows]
    return {"ok": True, "types": types,
            "totals": {"titles": sum(r["title_count"] for r in types),
                       "bytes": sum(r["total_bytes"] for r in types)}}


@router.get("/growth")
async def growth():
    p = db.pool()
    if p is None:
        return {"ok": False, "points": []}
    async with p.acquire() as c:
        rows = await c.fetch(
            """
            SELECT d, sum(title_count) AS titles FROM (
              SELECT DISTINCT ON (media_type, date_trunc('day', taken_at))
                     media_type, date_trunc('day', taken_at) AS d, title_count
              FROM library_snapshots
              ORDER BY media_type, date_trunc('day', taken_at), taken_at DESC
            ) x GROUP BY d ORDER BY d
            """
        )
    return {"ok": True, "points": [{"date": r["d"].date().isoformat(), "titles": int(r["titles"])} for r in rows]}
