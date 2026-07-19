# Single Sign-On with Authentik

The goal: one account per person, one place to manage access, and a login
experience that feels native in every app. Self-Hosted Media Platform uses
[Authentik](https://goauthentik.io/) as the identity provider (IdP) at
`id.example.org`.

The core lesson of this build is that **there is no single "best" SSO
protocol** — you pick per app based on how that app authenticates its clients.
Self-Hosted Media Platform uses three patterns.

## Authentik building blocks (vocabulary)

- **Provider** — implements a protocol (LDAP, OAuth2/OIDC, or Proxy/forward-
  auth). One per integration.
- **Application** — the user-facing object that points at a Provider and
  controls who may access it (via Bindings/policies).
- **Outpost** — a companion process that actually serves LDAP or forward-auth
  requests. Authentik ships an *embedded* outpost that handles proxy/forward-
  auth for you.
- **Flow** — a sequence of stages (login, enrollment, consent). You bind
  policies and stages onto flows to shape behavior.

## Deployment note

Run the official pinned Authentik compose stack (server + worker + Postgres +
Redis). Two things to get right up front:

- **Back up** the `.env` (contains the DB password and secret key) and the
  Postgres volume. Losing the secret key means re-enrolling everything.
- **Do not** set a custom `TZ` or mount host time into the Authentik containers.
  It can interfere with token/skew handling. Leave time on defaults.

---

## Pattern 1 — LDAP (Jellyfin)

**Why LDAP for Jellyfin:** Jellyfin's native login box exists on every client
(TVs, phones, consoles). With LDAP, users keep typing into that familiar box,
and — critically — **existing local profiles attach to LDAP identities by
matching username**, so watch history and settings are preserved during
migration. OIDC would have replaced the native login UX on some clients.

Setup:

1. In Authentik create an **LDAP Provider** and an **LDAP outpost**; the outpost
   listens on ports `389`/`636`. Give it a base DN, e.g.
   `dc=example,dc=media`.
2. In Jellyfin install the **LDAP-Auth** plugin and point it at the outpost:
   - Bind DN: a dedicated service account, e.g.
     `cn=ldap_bind,ou=users,dc=example,dc=media`
   - User search filter, e.g.
     `(memberOf=cn=jellyfin_users,ou=groups,dc=example,dc=media)`
   - Username attribute: `cn`

**Gotcha — the bind account can only see itself.** Recent Authentik removed the
provider's "Search group" field. Until you grant it directory-search rights, the
bind account authenticates but the user search returns nothing. Fix:

1. Create a **Role**, give it the *"Search full LDAP directory"* object
   permission on the LDAP provider, and attach that role to the `ldap_bind`
   account.
2. The outpost caches for a few minutes — set the outpost's search mode to
   *direct querying* or restart the LDAP outpost container to apply changes
   immediately.

## Pattern 2 — OIDC (Kavita)

**Why OIDC for Kavita:** Kavita has first-class OpenID Connect support and can
auto-provision accounts, so OIDC is the least-friction choice.

In Authentik create an **OAuth2/OIDC Provider** + Application. In Kavita's OIDC
settings:

- Authority / issuer: `https://id.example.org/application/o/kavita/`
- Redirect URI: `https://read.example.org/signin-oidc`
- Post-logout redirect: `https://read.example.org/signout-callback-oidc`
- Front-channel logout: `https://read.example.org/signout-oidc`
- Enable "provision accounts" so first-time OIDC users get a Kavita account
  automatically.

**Gotcha — issuer must match exactly.** Authentik derives the issuer from the
request host (via the reverse proxy). The Authority you configure in the client
must match the issuer Authentik emits, character-for-character, or validation
fails. Make sure the proxy forwards the correct host headers.

## Pattern 3 — Forward-auth (apps with no native login)

**Why forward-auth for internal tools:** some apps have no user system at all.
Instead of building one, the reverse proxy asks Authentik "is this request
authenticated?" before every request reaches the app.

Steps:

1. **Provider** → *Proxy Provider*, mode **Forward auth (single application)**,
   external host `https://tools.example.org`.
2. **Application** → name it, and set its **Provider** to the proxy provider
   above. (The Application's Provider dropdown is the only place the two link.)
3. **Outpost** → add the Application to the **embedded outpost** so it serves the
   `/outpost.goauthentik.io/*` endpoints for that host.
4. **Reverse proxy** → inject the forward-auth snippet into the proxy host:

```nginx
location /outpost.goauthentik.io {
    proxy_pass       http://<SERVER_IP>:9000/outpost.goauthentik.io;
    proxy_set_header Host $host;
    proxy_set_header X-Original-URL $scheme://$http_host$request_uri;
    add_header Set-Cookie $auth_cookie;
    auth_request_set $auth_cookie $upstream_http_set_cookie;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
}

# server-level (NOT wrapped in a second `location /`, which would duplicate
# the proxy host's auto-generated one):
auth_request     /outpost.goauthentik.io/auth/nginx;
error_page       401 = @goauthentik_proxy_signin;
auth_request_set $auth_cookie $upstream_http_set_cookie;
add_header       Set-Cookie $auth_cookie;
auth_request_set $authentik_username $upstream_http_x_authentik_username;
proxy_set_header X-authentik-username $authentik_username;
# (repeat auth_request_set/proxy_set_header for groups, email, name, uid)

location @goauthentik_proxy_signin {
    internal;
    add_header Set-Cookie $auth_cookie;
    return 302 /outpost.goauthentik.io/start?rd=$request_uri;
}
```

**Gotcha — the outpost's `authentik_host`.** The embedded outpost redirects
users to the OAuth authorize endpoint using its configured `authentik_host`. If
you migrated domains, this can still point at the old one, sending users to a
dead/incorrect login URL. Set it to the current public IdP URL in the outpost
configuration:

```yaml
authentik_host: https://id.example.org/
```

**Note — forward-auth only gates the front door.** Internal callers (like the
Discord bot) hit the app's container address directly, bypassing the proxy and
therefore the auth. That's intentional here (the bot is trusted), but it means
forward-auth is *perimeter* auth, not per-request authorization inside the app.

## Pattern 4 — Delegated login (Jellyseerr)

**Why delegated login for Jellyseerr:** it has a built-in "Sign in with
Jellyfin" option that authenticates directly against Jellyfin's own user API.
Since Jellyfin is already backed by the Authentik LDAP outpost (Pattern 1),
Jellyseerr transitively trusts Authentik without ever talking to it.

This needs **zero Authentik configuration** — no Provider, no Application, no
outpost binding. Jellyseerr just validates the entered credentials against
Jellyfin, and Jellyfin does what it always does. It's the cheapest of the four
patterns, and it's only available because Pattern 1 already exists: an app can
delegate to another app's login, but that other app still has to be wired to
the real identity source somewhere.

The tradeoff is coupling — if Jellyfin's auth path ever breaks or changes,
Jellyseerr logins break with it, silently, since there's nothing in Authentik
itself to point at.

---

## Enrollment / onboarding

New members self-serve via a Discord OAuth **social source**:

- Callback: `https://id.example.org/source/oauth/callback/discord/`
- An **enrollment flow** turns a Discord login into an Authentik account. Its
  prompt stage collects a **password** (needed because Jellyfin/Jellyseerr
  authenticate via LDAP, which requires one; Kavita's OIDC would work without).
- A group-assignment **policy** on the flow's write stage drops new users into
  the right groups automatically (e.g. `jellyfin_users`, `media`).

## Access control with groups

Groups are the authorization primitive:

- `jellyfin_users` — gates the Jellyfin LDAP search filter (only members can
  log in to Jellyfin).
- `media` — a general "has access" group.
- `*_admins` — elevated access where needed.

To restrict a forward-auth app to a subset of users, add a **Binding** (policy
or group) on its Application — e.g. allow only `media`.

## Choosing a pattern (summary)

| If the app… | Use | Because |
|---|---|---|
| has a beloved native login box on many clients | **LDAP** | keeps that UX; can preserve existing profiles |
| speaks OIDC and can auto-provision | **OIDC** | least friction, modern, self-service |
| has no auth of its own | **Forward-auth** | proxy gates it; zero app changes |
| trusts another app's identity | **delegated login** | avoids duplicate accounts |

## Why run a full IdP for a handful of users

Authentik is a heavier operational commitment than the group size strictly
needs — it's its own Postgres, its own Redis, its own upgrade cadence, for a
household-scale member list. It earns that cost because it's the one piece
that made four different auth requirements collapse into one account per
person, one place to fix a lockout, and one enrollment flow instead of four.
A lighter option (a shared reverse-proxy basic-auth password, or letting each
app manage its own users) would have been less to run, but every app would
need its own account, and "reset someone's password" would mean knowing which
of four systems actually holds it. That tradeoff — real operational weight,
in exchange for one identity instead of four — is the actual decision this
document is documenting.
