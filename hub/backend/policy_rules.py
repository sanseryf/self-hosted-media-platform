"""Pure policy evaluation for the Jellyseerr auto-approver — DB-free, unit-tested.

Given a request's context and a rule config, decide approve / route-to-manual /
deny, always with the reasons recorded (auditability is the point). This is the
decision engine; the processor in projects/policy.py gathers the context (best
effort, via the L2 gateway) and — only when POLICY_ENFORCE is on — acts on it.

The default posture is conservative: unless a request clearly clears every gate,
it goes to a human (manual), never auto-denied silently.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Rules:
    max_pending_per_user: int = 10        # too many outstanding requests → hold
    min_disk_free_gb: float = 50.0        # under this headroom → hold
    auto_approve_media_types: tuple = ("movie", "series")  # types eligible for auto-approve at all
    deny_media_types: tuple = ()          # types to refuse outright
    allow_4k: bool = False                # 4k requests always go to manual unless allowed


@dataclass
class PolicyDecision:
    action: str                            # 'approve' | 'manual' | 'deny'
    reasons: list = field(default_factory=list)


# Events arrive with media_type already normalized ("tv" -> "series", see
# event_sources._media_type), but rule config is written by hand and may use
# either word. Canonicalize both sides so "tv" and "series" mean the same thing.
_ALIASES = {"tv": "series", "show": "series", "film": "movie"}


def _canon(mt) -> str:
    mt = (mt or "").lower()
    return _ALIASES.get(mt, mt)


def evaluate(ctx: dict, rules: Rules) -> PolicyDecision:
    """ctx keys (all optional; missing → that gate is skipped, noted in reasons):
        media_type: 'movie'|'series' (aliases like 'tv' accepted)
        is_4k: bool
        pending_count: int      (requester's current outstanding requests)
        disk_free_gb: float     (headroom on the target *arr root)
    """
    reasons: list[str] = []
    mt = _canon(ctx.get("media_type"))

    # Hard deny first.
    if mt and mt in {_canon(t) for t in rules.deny_media_types}:
        return PolicyDecision("deny", [f"media_type '{mt}' is denied by policy"])

    # Gates that route to manual (a human decides) rather than deny.
    if mt and mt not in {_canon(t) for t in rules.auto_approve_media_types}:
        reasons.append(f"media_type '{mt}' not in auto-approve list")

    if ctx.get("is_4k") and not rules.allow_4k:
        reasons.append("4k request requires manual approval")

    pending = ctx.get("pending_count")
    if pending is not None and pending >= rules.max_pending_per_user:
        reasons.append(f"requester has {pending} pending (limit {rules.max_pending_per_user})")

    free = ctx.get("disk_free_gb")
    if free is not None and free < rules.min_disk_free_gb:
        reasons.append(f"disk headroom {free:.0f}GB below floor {rules.min_disk_free_gb:.0f}GB")

    if reasons:
        return PolicyDecision("manual", reasons)
    return PolicyDecision("approve", ["clears all gates"])
