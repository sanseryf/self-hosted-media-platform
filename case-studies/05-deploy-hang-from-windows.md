# Deploys hung at "uploading", for two different reasons

**Type:** tooling failure · **Area:** deployment (PowerShell on Windows →
Linux over SSH) · **Status:** both causes fixed

## Summary

The one-command deploy (`mf deploy`) started hanging at step 1 of 3,
*uploading files to staging*. Small SSH commands still worked, and a single
2 MB test upload took half a second. It turned out to be two separate bugs
stacked on top of each other: fixing the first one made the problem seem
solved until the next large deploy.

## Cause 1: a global console-encoding change

An earlier change set PowerShell's console output encoding to UTF-8 for the
**whole** run, to fix garbled characters in output coming back from the
server. Windows' bundled OpenSSH `scp` stalled under that setting.

**Fix:** UTF-8 decoding is now scoped to the one function that reads remote
output. It's set on entry and restored in a `finally` block.

**Rule adopted:** never change console encoding globally in the deploy tool.

## Cause 2: one `scp` carrying 40+ files

The next tools deploy (44 files) hung again, with cause 1 already fixed.
Single-file uploads stayed fast. A multi-file `scp` from Windows was the
flaky part.

**Fix:** pack, then send one file.

```mermaid
flowchart LR
    A[Files to deploy] -->|built-in Windows tar,<br/>excludes caches| B[one .tgz]
    B -->|single scp,<br/>the path the health check proves| C[/tmp on server/]
    C -->|unpack into staging,<br/>delete the archive| D[staging dir]
    D -->|UNPACKED marker present?| E[continue deploy]
```

If `tar` isn't available, it falls back to the old multi-file upload, still
under a time limit. This was tested end to end with fake `ssh`/`scp`
commands: files deployed and checked, no temp files left behind.

## The red herring

A VPN MTU mismatch was suggested as the cause. It was harmless and unrelated.
Ruling it out was quick because a single-file upload over the same path
worked fine.

## Hardening kept

- **Wall-clock limits on every upload** (120 s for tools, 300 s for the
  console). A stall now fails loudly and says that nothing on the server
  changed.
- **Step markers** (`0/3 checking the connection`, `1/3 upload …`, then
  `packed N items into one X KB file`), so the next hang points at a step.
- **A `doctor` command** that includes a 2 MB upload test with a 30 s limit.

## Lessons

- **When a fix "works", check it against the case that failed.** Here it was
  the large deploy, not the small one.
- **Global state changes in a script leak into child processes** you didn't
  think about.
- **Bound every network call in time.** A hang with no limit gives you
  nothing to debug.
