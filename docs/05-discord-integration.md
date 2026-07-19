# Discord Integration

A single Discord bot (built on `discord.py`, code name **cinema-bot**; see the
[deployment runbook](06-deployment-runbook.md#3-discord-bot--configure--start-server))
is the group's chat-native front door. It does two jobs: help members find
things to watch, and let them finish their own account setup without an admin.

## Why a bot

The group already lives in Discord. A bot means "is this in the library?" or
"I need a password" is one slash command away, with no browser and no admin in
the loop.

## Commands

| Command | Backend | Action |
|---|---|---|
| `/movie`, `/show`, `/anime` | TMDB + Jellyfin | Look up a title, show poster and synopsis, note whether it's already in the library |
| `/random`, `/trending`, `/trailer`, `/similar` | TMDB / Jellyfin | Discovery helpers |
| `/setup` | Authentik | Self-service onboarding: confirms the member's Discord account is linked to an enrolled user, grants the **Member** role, and replies privately with a one-time link to set a password |

Movie and TV *requests* stay with Jellyseerr, which owns that workflow. The bot
links to it rather than duplicating it.

## `/setup`, in order

1. Defer the interaction privately (only the member sees the reply).
2. Look up the member's Discord ID among the SSO provider's linked accounts.
   That lookup **is** the authorization: no link, no role, no password link.
3. Grant the Member role, which unlocks the request channels. This is
   idempotent (skipped if they already hold it) and runs *before* step 4, so a
   flaky recovery endpoint can't also leave a verified member locked out of
   the channels.
4. Mint a recovery link and send it in the private reply.

If Discord refuses the role grant (missing permission, or the bot's role is
ranked too low), the member still gets their password link plus a one-line
note, and the log names both possible causes. The story behind this design is
in the [onboarding case study](../case-studies/01-onboarding-role-gap.md).

## Design notes

- **Least privilege.** The bot needs *Manage Roles* with its own role ranked
  above Member, and an SSO API token scoped to reading users and creating
  recovery links. Nothing else.
- **Config via environment.** The bot reads its Discord token, a TMDB key and
  the SSO token from its `.env`. An optional guild id makes slash commands
  register instantly instead of waiting on global propagation.
- **Private by default.** Account replies are ephemeral; nothing about a
  member's account is ever posted in a channel.

## Possible improvements

- **An admission gate.** Enrollment is currently open Discord login at the SSO
  provider. An invitation stage in the enrollment flow would restore an
  explicit gate.
- **A `/status` command** that surfaces service health in-channel.
- **Rate limiting** on lookups, so one member can't exhaust the TMDB quota.
