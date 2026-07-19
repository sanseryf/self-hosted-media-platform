# Containerization on Linux

Every service runs as a Docker container on an Ubuntu host, orchestrated with
Docker Compose. This document covers the conventions that make the stack
reproducible and easy to reason about.

## Host baseline

- Ubuntu LTS, Docker Engine + the Compose plugin (`docker compose`, not the
  legacy `docker-compose`).
- A dedicated non-root user (uid/gid `1000`) owns the media data.
- A single data disk mounted at `/library` holds everything: the media tree
  and per-app config.

## Convention 1 — one folder per stack

Each service lives in its own directory with a single `docker-compose.yml`:

```
/library/
├── jellyfin/docker-compose.yml
├── kavita/docker-compose.yml
├── authentik/docker-compose.yml
├── sonarr/docker-compose.yml
├── radarr/docker-compose.yml
├── reverse-proxy/docker-compose.yml
└── hub/                   # the custom app (also its own stack)
```

Why: each stack starts/stops/updates independently, its config is version-
controllable in isolation, and there's zero ambiguity about where a service is
defined. It's "infrastructure as folders" — humble, but it scales fine for a
single host and is trivial to back up.

## Convention 2 — one shared library mount

Every container that touches media mounts the **same** host path at the **same**
container path:

```yaml
volumes:
  - /library:/data
```

So `/data` means the same thing everywhere. The library managers import into
`/data/media/{Movies,TV,Books,Comics}`; the media servers read the same
tree. Because the path is identical across containers, imports are atomic moves
(same filesystem) instead of slow cross-mount copies, and there's no path
remapping to misconfigure.

> Rule of thumb: keep every media-handling container on one shared mount with a
> consistent internal path. Most "the file is there but nothing imported"
> problems trace back to mismatched paths.

## Convention 3 — fix bind-mount ownership

Docker creates missing bind-mount source directories as `root`. A container
that runs as a non-root user (the LinuxServer.io images use `PUID`/`PGID`) then
can't write to them and crashes on start. Fix by pre-creating/owning the dir:

```bash
sudo mkdir -p /library/<new-service>
sudo chown -R 1000:1000 /library/<new-service>
```

The LinuxServer images take `PUID=1000` / `PGID=1000` in their environment so
files land with the right ownership.

## A representative compose file

```yaml
services:
  kavita:
    image: jvmilazz0/kavita:latest
    container_name: kavita
    restart: unless-stopped
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=<TZ>
    volumes:
      - ./config:/config
      - /library:/data
    ports:
      - "5000:5000"
```

Notes that generalize to every stack:

- `restart: unless-stopped` so services survive reboots.
- Config is a bind mount (`./config`) next to the compose file — easy to back up.
- Secrets come from a `.env` file (`env_file: .env`), never committed. A
  committed `.env.example` documents the required keys.
- Set `TZ` on app containers for correct timestamps — **except** identity
  services like Authentik, where mounting host time / setting TZ can interfere
  with token validity. Leave those on defaults.

## Secrets pattern

```
service/
├── docker-compose.yml     # committed
├── .env.example           # committed — documents keys, no values
└── .env                   # gitignored — real values
```

`docker-compose.yml` references `env_file: .env`; the app reads config from
environment variables. This keeps credentials out of version control and lets
the same compose file work in any environment.

## Deploy workflow

From a workstation, push the stack folder and bring it up:

```powershell
# 1) copy the stack to the server (PowerShell/scp)
scp -r ".\hub" <user>@<SERVER_IP>:/library/
```

```bash
# 2) on the server
cd /library/hub
cp .env.example .env && nano .env      # fill in secrets
docker compose up -d --build
```

**Gotcha — pasting into a fresh SSH session.** Multi-line pastes made
immediately after an SSH session opens can drop the first lines. Two reliable
workarounds:

- Open the session first, wait for the prompt, *then* paste; or
- Wrap server-side commands in a single here-doc so they execute atomically:

```bash
bash <<'EOF'
cd /library/hub
docker compose up -d --build
curl -s http://localhost:8090/api/health
EOF
```

## Verifying a deploy

Prefer a real health signal over "the container is running":

```bash
docker compose ps                       # state
docker logs --tail 50 <container>       # recent output
curl -s http://<SERVER_IP>:<port>/health   # app-level readiness
```

A container can be "up" while the app inside is misconfigured; always hit an
endpoint that exercises the app's real dependencies.

## Where this stops scaling

"Infrastructure as folders" is deliberately low-tech, and that's the right
call for one host and one operator — no Terraform state to manage, no
orchestrator to learn, `scp` and `docker compose up -d` are the entire deploy
surface. It stops being the right call once either grows: more than one host
needs shared secret management and a real inventory instead of per-folder
`.env` files, and more than one operator needs the compose files' history to
live somewhere more disciplined than "whatever's currently on the server plus
whatever's in git." Neither has been true here yet.
