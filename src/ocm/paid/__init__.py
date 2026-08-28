"""The paid half of the loop.

Deliberately NOT pre-abstracted into a universal ad-platform interface. See `platform.py`
for the reasoning: an interface only gets as wide as two real implementations prove. What
is portable is the DATA (`Campaign`, `MetricSnapshot`) and the SAFETY MECHANISM (the intent
digest, the spend gate, the append-only store), and those are what this package defines.
"""

from .campaign import Campaign, CreativeRef, Guardrails, StopClock
from .collector import Collector, MetricSnapshot, SnapshotStore, extract_action, to_snapshots
from .creative import (
    BaseDirUnknown,
    CreativeError,
    CreativeHashMismatch,
    CreativeRead,
    assert_all_ok,
    load_creatives,
    read_creative,
    resolve,
)
from .intent import canonical_instant, intent_digest, render_review_card
from .platform import CampaignState, DryRunPlatform, OutcomeUnknown, PlatformRefused
from .spend_gate import SpendGrant, SpendRefused, authorize

__all__ = [
    "Campaign",
    "CreativeRef",
    "Guardrails",
    "StopClock",
    "Collector",
    "MetricSnapshot",
    "SnapshotStore",
    "to_snapshots",
    "extract_action",
    "CreativeRead",
    "CreativeError",
    "BaseDirUnknown",
    "CreativeHashMismatch",
    "resolve",
    "read_creative",
    "load_creatives",
    "assert_all_ok",
    "intent_digest",
    "canonical_instant",
    "render_review_card",
    "CampaignState",
    "DryRunPlatform",
    "PlatformRefused",
    "OutcomeUnknown",
    "authorize",
    "SpendGrant",
    "SpendRefused",
]
