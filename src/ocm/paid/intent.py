"""The intent digest: what a human approval is actually bound to.

A rendered review document is what the human READ. It is not what they approved. Those
differ in a way that matters, because the document contains a "generated at" timestamp and
other presentation detail, so re-rendering it never reproduces the same bytes, and a digest
of it cannot be re-derived at spend time.

So the binding is a digest of the MATERIAL INTENT: every field that changes what is
actually bought, and nothing else.

    campaign id, objective, optimization event, landing url,
    lifetime budget, currency, start and end instants,
    sorted geo, sorted languages, sorted creative content hashes

TWO PROPERTIES, EACH LOAD-BEARING

IT IS DETERMINISTIC, NOT RANDOM. A random nonce would mean that a retry after a timeout
computes a different value, so the first attempt's campaign becomes unfindable and the
retry creates a SECOND real campaign spending real money. A deterministic digest lets the
retry look up what the first attempt may have created and adopt it instead of duplicating
it. Determinism here is a double-spend control, not a convenience.

IT EXCLUDES ANYTHING THAT VARIES BY MACHINE. The config's directory path is deliberately
NOT in the digest. It is an absolute path that differs between a laptop and a server, or
between two checkouts on one laptop. Including it would make the same campaign compute a
different digest from a different working directory, which defeats adoption and creates the
duplicate campaign the determinism was supposed to prevent.

Timestamps are canonicalized to UTC and truncated to the minute for the same reason: two
hosts with slightly different clocks must agree on the digest of one intent.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from .campaign import Campaign

# Field separator that cannot appear in any of the values being joined, so that no
# combination of field contents can be re-parsed as a different combination.
_SEP = "\x1f"


def canonical_instant(dt: datetime) -> str:
    """UTC, truncated to the minute. Naive datetimes are refused, never assumed local."""
    if dt.tzinfo is None:
        raise ValueError(
            "refusing a timezone-naive instant: two hosts would disagree on its meaning"
        )
    utc = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return utc.strftime("%Y-%m-%dT%H:%MZ")


def intent_digest(
    campaign: Campaign, *, starts_at: datetime, ends_at: datetime
) -> str:
    """The 32-hex-character digest that an approval binds to.

    Every field here changes what is bought. Anything not here does not.
    """
    if ends_at <= starts_at:
        raise ValueError("ends_at must be after starts_at")
    g = campaign.guardrails
    material = _SEP.join(
        [
            campaign.campaign_id,
            campaign.objective,
            campaign.optimization_event,
            campaign.landing_url,
            str(g.lifetime_budget_minor),
            g.currency,
            g.decision_metric,
            canonical_instant(starts_at),
            canonical_instant(ends_at),
            ",".join(sorted(campaign.geo)),
            ",".join(sorted(campaign.languages)),
            # Sorted, so that reordering the creatives in config is correctly recognized
            # as the same intent rather than as a new campaign.
            ",".join(sorted(c.content_hash for c in campaign.creatives)),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def render_review_card(
    campaign: Campaign, *, starts_at: datetime, ends_at: datetime, digest: str
) -> str:
    """The human-readable document. Presentation only: the digest is the binding.

    Note what is printed for each creative: its approved content hash, and its ref. Not a
    hash recomputed at render time. Printing a freshly computed digest would show the
    reviewer the bytes currently on disk while the approval binds the bytes that were
    approved, which is the wrong way round.
    """
    g = campaign.guardrails
    lines = [
        "APPROVAL REQUIRED before this campaign may spend",
        "=" * 62,
        f"campaign            {campaign.campaign_id}",
        f"objective           {campaign.objective}",
        f"optimization event  {campaign.optimization_event}",
        f"landing url         {campaign.landing_url}",
        f"lifetime budget     {g.lifetime_budget_minor} minor units {g.currency}",
        f"decision metric     {g.decision_metric}  (pre-registered)",
        f"flight              {canonical_instant(starts_at)} to {canonical_instant(ends_at)}",
        f"geo                 {', '.join(sorted(campaign.geo)) or 'unset'}",
        f"languages           {', '.join(sorted(campaign.languages)) or 'unset'}",
        "creatives:",
    ]
    for c in sorted(campaign.creatives, key=lambda c: c.ref):
        lines.append(f"  - {c.ref}")
        lines.append(f"      approved hash {c.content_hash}")
    lines += [
        "",
        f"intent digest       {digest}",
        "",
        "Approving binds your decision to the digest above. If any field on this card",
        "changes, the digest changes and the approval is refused at spend time.",
    ]
    return "\n".join(lines)
