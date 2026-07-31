# L4 — Member Surfaces

The user-facing payoff of the L1–L3 stack. **Admin surfaces (DLQ browser, drain/
reconcile console, policy log viewer) are intentionally NOT here** — those are
slated for the devops-console refresh. These two are member-friendly and built on
the site's own Nocturne tokens.

## Files

| File | Role |
|---|---|
| `backend/static/tokens.css` | the Nocturne token system, shared so these pages are visually identical to `index.html` and follow all six `[data-theme]` palettes |
| `backend/static/wall.html` | live **Now Playing** wall — SSE-driven, one card per active stream |
| `backend/static/journey.html` | **Request Journey** — per-title state stepper (requested→played) + expandable transition timeline, filtered to the signed-in user |
| `backend/projects/watching.py` | added `GET /api/watching/stream` (SSE) |

Served at `/static/wall.html` and `/static/journey.html` (the existing `StaticFiles`
mount). Their data APIs are session-gated like everything else, so a logged-out
visitor gets a friendly "sign in" state.

## How they stay on-theme (and future-proof)

`tokens.css` is copied verbatim from `index.html`'s `:root` + `[data-theme]` blocks,
and the pages use **only semantic tokens** (`--accent`, `--ok`/`--warn`/`--down`,
`--surface`/`--raised`, `--grad-progress`, spacing/radius/type scales) — zero
hardcoded colors. On load each page reads `localStorage['mf-theme']` and applies the
same `data-theme` attribute the switcher uses, so it renders in the visitor's chosen
palette. Retune a token → these pages update with the site.

> Duplication note: `index.html` still has its own inline copy of the tokens (its
> design pass is fresh; not worth a risky surgical edit now). A later consolidation
> can have `index.html` `<link>` `tokens.css` and drop the inline block. Keep the two
> in sync until then.

## The live wall (SSE)

`GET /api/watching/stream` holds one server-side Jellyfin poll loop and pushes the
same payload as `/now` every `WALL_STREAM_SECONDS` (default 4) over a single SSE
connection — the browser doesn't hammer `/now`. The page prefers `EventSource`; if
the browser or endpoint won't stream it **falls back to polling `/now`** every 5s.
Ends cleanly on client disconnect.

## Verification done

Served the static dir and checked in-browser (no live data needed for the shell):
- tokens resolve — `--accent` `#FF2E9A`, body `--void`, progress bar `--grad-progress`,
  live dot `--ok`, headings in the Hoefler serif;
- **palette switching works** — default→ember→phosphor all re-resolve `--magenta`;
- render paths work — wall renders session cards (progress %, badges, times); journey
  renders 6-step steppers with active/done/stalled/failed states and flags.

Not exercised here (needs the running hub + live Jellyfin/DB): real SSE frames and
the gated fetches. Those come up at staging/deploy.

## Not wired: nav links

The pages exist and render but aren't yet linked from `index.html`'s nav (left that
edit out to avoid touching the freshly-designed homepage). Add links to
`/static/wall.html` and `/static/journey.html` wherever fits, or ask and I'll place them.
