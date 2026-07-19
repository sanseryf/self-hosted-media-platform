# Reverse Proxy & TLS

One reverse proxy is the single public entry point. It terminates TLS and routes
each subdomain to the right container. Self-Hosted Media Platform uses Nginx Proxy Manager
(NPM) — a web UI over nginx — but the concepts map to raw nginx, Traefik, or
Caddy.

## DNS

- A wildcard record `*.example.org` and the apex `@` both point at the home
  public IP.
- Records are **DNS-only** (no CDN proxy). A general-purpose CDN can't carry
  media streams well, and some CDN terms of service prohibit proxying video.
- Residential IPs change, so a dynamic-DNS container watches the IP and updates
  the DNS record via the provider's API:

```yaml
services:
  ddns:
    image: favonia/cloudflare-ddns:latest
    restart: unless-stopped
    environment:
      - CLOUDFLARE_API_TOKEN=<SCOPED_DNS_EDIT_TOKEN>
      - DOMAINS=example.org
      - PROXIED=false
```

The API token is scoped to *DNS edit* on the one zone — least privilege, and
easy to rotate.

## TLS — one wildcard cert

Rather than a certificate per subdomain, issue a single wildcard
`*.example.org` via the **DNS-01** ACME challenge (Let's Encrypt). DNS-01 proves
control of the domain by creating a TXT record, so it works for wildcards and
doesn't require any inbound HTTP to each host. NPM automates issuance and renewal
once you give it the same scoped DNS API token.

Result: add a new subdomain any time and it's already covered by the existing
cert — no per-app certificate dance.

## Proxy hosts

Each app is a "proxy host": a subdomain forwarded to a container's LAN
address\:port.

| Subdomain | Forwards to | Service |
|---|---|---|
| `example.org` (apex) | `<SERVER_IP>:<HUB_PORT>` | Hub (landing page + data platform, see [Data platform](07-data-platform.md)) |
| `watch.example.org` | `<SERVER_IP>:8096` | Jellyfin |
| `read.example.org` | `<SERVER_IP>:5000` | Kavita |
| `request.example.org` | `<SERVER_IP>:5055` | Jellyseerr |
| `id.example.org` | `<SERVER_IP>:9000` | Authentik |

Standard per-host settings: **Force SSL**, **HTTP/2**, **Block Common
Exploits**, and **Websockets Support** on (Jellyfin and others need WS for live
features).

## Custom nginx for forward-auth

Apps without their own login (internal tools) are gated by injecting an
`auth_request` block into the proxy host's advanced/custom nginx config, which
delegates every request to Authentik's outpost. The full snippet and the
reasoning live in the [SSO doc](04-sso-with-authentik.md#forward-auth-for-apps-with-no-native-login).

Two practical notes from doing this in NPM:

- The custom-config box injects into the `server` block, which already contains
  an auto-generated `location /`. Put server-level `auth_request*` directives at
  the top level (not wrapped in a second `location /`) to avoid a duplicate-
  location error; keep the `/outpost.goauthentik.io` and sign-in blocks as their
  own `location`s.
- A browser error like `SSL_ERROR_UNRECOGNIZED_NAME` on a brand-new host usually
  means nginx hasn't (re)loaded a valid server block for that name yet — often
  the cert is still issuing, or a config test failed. Verify with
  `docker exec <npm> nginx -t` and re-check the cert before assuming the app is
  broken.

## Why NPM, not raw nginx/Traefik/Caddy

NPM is a thin web UI over real nginx config, not a different reverse proxy —
so nothing here is locked in. It won for this stack because the audience is
one operator who wants proxy hosts and cert renewal to be a form, not a config
file to hand-edit under time pressure. The custom-nginx escape hatch (used
above for forward-auth) means it never actually blocks anything raw nginx
could do; it's a UI on top of the same tool, not a ceiling. Traefik's
label-driven config would fit a Compose-heavy, frequently-changing stack
better, and Caddy's automatic HTTPS is the simplest of the three for a
single-host setup with no wildcard/DNS-01 requirement — either would be a
reasonable swap if the "one operator, occasional changes" assumption stopped
holding.
