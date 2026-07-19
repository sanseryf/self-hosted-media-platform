# Deployment Runbook

End-to-end deploy of the custom pieces (Hub and the Discord bot), with
placeholders. Replace every `<ANGLE_BRACKET>` value and `example.org`.
Server-side blocks are here-docs, so they run atomically even if pasted right
after an SSH connect.

## Values you need to supply

| Placeholder | Where to get it |
|---|---|
| `<SERVER_IP>` | Your server's LAN IP |
| `<SSH_USER>` | Your server login |
| `<PG_PASSWORD>` | The password for Hub's Postgres user |
| `<JELLYFIN_API_KEY>` | Jellyfin → Dashboard → API Keys |
| `<SONARR_API_KEY>` / `<RADARR_API_KEY>` | Each app → Settings → General → API Key |
| `<JELLYSEERR_API_KEY>` | Jellyseerr → Settings → General → API Key |
| `<EVENTS_TOKEN>` | Any long random string (`openssl rand -hex 32`); the shared secret webhooks present |
| `<DISCORD_TOKEN>` | Discord Developer Portal → your bot |
| `<TMDB_API_KEY>` | themoviedb.org account settings |
| `<AUTHENTIK_TOKEN>` | Authentik → Directory → Tokens, scoped to what the bot needs |

## 1. Upload the stacks (workstation)

```powershell
$SRV = "<SSH_USER>@<SERVER_IP>"
scp -r ".\hub"        "${SRV}:/library/"
scp -r ".\cinema-bot" "${SRV}:/library/discord-media-stack/"
```

## 2. Hub — configure & start (server)

```bash
bash <<'EOF'
cd /library/hub
cat > .env <<ENV
DATABASE_URL=postgresql://hub:<PG_PASSWORD>@postgres:5432/hub
JELLYFIN_URL=http://<SERVER_IP>:8096
JELLYFIN_API_KEY=<JELLYFIN_API_KEY>
SONARR_URL=http://<SERVER_IP>:8989
SONARR_API_KEY=<SONARR_API_KEY>
RADARR_URL=http://<SERVER_IP>:7878
RADARR_API_KEY=<RADARR_API_KEY>
JELLYSEERR_URL=http://<SERVER_IP>:5055
JELLYSEERR_API_KEY=<JELLYSEERR_API_KEY>
EVENTS_TOKEN=<EVENTS_TOKEN>
STATUS_HOST=<SERVER_IP>
ENV
chmod 600 .env
docker compose up -d --build
sleep 6
curl -s http://<SERVER_IP>:8090/api/health
EOF
```

A healthy response is `{"ok":true,"db":{"ok":true,…}}`. Then point each
upstream's webhook at Hub. The URLs and payload settings are in
[`hub/EVENTS-L1.md`](../hub/EVENTS-L1.md).

## 3. Discord bot — configure & start (server)

```bash
bash <<'EOF'
cd /library/discord-media-stack/cinema-bot
cat > .env <<ENV
DISCORD_TOKEN=<DISCORD_TOKEN>
TMDB_API_KEY=<TMDB_API_KEY>
AUTHENTIK_URL=http://<SERVER_IP>:9000
AUTHENTIK_TOKEN=<AUTHENTIK_TOKEN>
ENV
chmod 600 .env
docker compose up -d --build
docker logs --tail 5 cinema-bot
EOF
```

> Redeploying later? Re-running the `cat > .env` block overwrites the file. If
> you've since hand-edited it (for example, to add a guild id for instant
> slash-command registration), edit the file instead of regenerating it.

## 4. Publish + protect (reverse proxy + SSO)

1. Add a proxy host for the apex `example.org` → `http://<SERVER_IP>:8090`
   (Force SSL, HTTP/2, wildcard cert, Websockets on; the live wall uses SSE).
2. Hub signs members in itself, against Jellyfin, so it needs no forward-auth.
   Any internal tool without its own login gets the forward-auth pattern from
   [SSO](04-sso-with-authentik.md#pattern-3--forward-auth-apps-with-no-native-login).

## 5. Verify end-to-end

```bash
# webhook ingest accepts a signed event and rejects an unsigned one
curl -s -X POST "http://<SERVER_IP>:8090/api/events/ingest/radarr" \
  -H "X-Hub-Token: <EVENTS_TOKEN>" -H 'Content-Type: application/json' \
  -d '{"eventType":"Test","movie":{"title":"Test","tmdbId":1}}'
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  "http://<SERVER_IP>:8090/api/events/ingest/radarr" -d '{}'      # expect 401
```

Then sign in at `https://example.org` and request something through
Jellyseerr. It should appear on `/static/journey.html` as the webhooks arrive.

## Redeploy after a code change

```powershell
scp -r ".\hub\backend" "<SSH_USER>@<SERVER_IP>:/library/hub/"
```
```bash
cd /library/hub && docker compose up -d --build --force-recreate
```

`restart` isn't enough: it reuses the old container, built from the old code
and the old compose config.

## Why copy-paste, not CI/CD

This is manual on purpose: there's one operator and deploys are infrequent, and
a runbook that's honest about every step is easier to trust than a pipeline
that hides them. The here-doc pattern exists so a paste-then-walk-away deploy
can't fail halfway from a dropped line. The trade-off flips once deploys get
frequent enough that "did I remember every step" becomes the real risk. At that
point, the natural next step is promoting this runbook into a script (or GitHub
Actions running the same commands), not a rewrite.
