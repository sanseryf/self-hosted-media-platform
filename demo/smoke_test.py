"""Smoke test for a running demo: sign in, then check every screen has data.

    python demo/smoke_test.py [http://localhost:8080]

Exits non-zero on the first failed check. CI runs this against the compose
stack, so a change that breaks the demo breaks the build.
"""
import json
import sys
import urllib.request
from http.cookiejar import CookieJar

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def call(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    with opener.open(req, timeout=15) as r:
        return json.loads(r.read())


def check(name, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        sys.exit(1)


check("sign in as demo/demo", call("/api/auth/login", {"username": "demo", "password": "demo"}).get("ok"))
check("library has titles", len(call("/api/library/items?limit=50")["items"]) >= 20)
check("three live sessions on the wall", len(call("/api/watching/now")["sessions"]) == 3)
titles = call("/api/lifecycle/titles?limit=200")["titles"]
check("lifecycle tracks 10 titles (TV requests stitched, not split)", len(titles) == 10)
check("one title is stalled", len(call("/api/lifecycle/stalled")["titles"]) >= 1)
kinds = {f["kind"] for f in call("/api/reconcile/findings")["findings"]}
check("reconciler found the planted drift",
      {"missing_from_jellyfin", "orphan_in_jellyfin"} <= kinds)
check("policy decision was recorded", len(call("/api/policy/decisions")["decisions"]) >= 1)
check("uptime board has 30 days", len(call("/api/stats/status/timeline?days=30")["services"][0]["days"]) >= 29)
print("demo smoke test passed")
