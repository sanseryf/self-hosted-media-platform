# Case studies

Write-ups of real incidents and fixes from running this platform, adapted
from the project's internal postmortems. Hosts, addresses, names and anything
else identifying have been removed. The technical detail hasn't.

The work was done AI-assisted: I operate the server and make the calls, with
Claude as a pairing partner for investigation, code and write-ups. That's
also how the rest of this repo was built.

| # | Case | Type | The short version |
|---|---|---|---|
| 1 | [New members could sign in but couldn't see anything](01-onboarding-role-gap.md) | Silent regression | A flow migration dropped a side effect, and the health check was still testing the old flow |
| 2 | [Total outage: the only network link was WiFi](02-network-loss.md) | Full outage | The admin path rode the same failing link, and the docs said the server was wired |
| 3 | [Media server down 16 hours after a kernel upgrade](03-gpu-driver-after-kernel-upgrade.md) | Outage + silent failure | The GPU driver didn't follow the kernel, and ~200 log lines went unread |
| 4 | [The SSO database was never in the backups](04-backup-gap-and-restore-drill.md) | Latent data-loss risk | Found by building a drill that actually restores the backups |
| 5 | [Deploys hung at "uploading", for two reasons](05-deploy-hang-from-windows.md) | Tooling failure | A global encoding change, then a flaky multi-file `scp` |
| 6 | [Building a demo found two production bugs](06-demo-found-two-production-bugs.md) | Latent correctness bugs | A type-vocabulary mismatch split TV requests; a partial index silently broke the audit log |

## The common thread

Most of these are the same failure in different clothes:

> **Something reported success from data that was missing, stale, or never
> checked.**

A health check that tested a retired flow. A dashboard that was green while a
container logged its own death every five minutes. A backup marked complete
without the database it existed to protect. A policy run that reported
"approve: 1" and stored nothing.

The rule I've taken from it, and now build to: **an all-clear is only honest
when there was something to clear, and a check that couldn't run has to say
so rather than show green.**

## Format

Each write-up follows a blameless postmortem shape: summary, impact, timeline
where it matters, root cause, why it wasn't caught sooner, the fix, how the
fix was verified, follow-ups, and lessons. Wrong turns are kept in, because
how a diagnosis went wrong is usually the most reusable part.
