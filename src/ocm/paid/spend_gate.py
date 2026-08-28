"""The gate between a human's approval and money moving.

A valid signature is where this check STARTS, not where it ends.

The token proves a human approved an intent digest. It does not prove that the campaign
sitting on the ad platform right now is still that intent. Between approval and spend, the
campaign could have been deleted and recreated under the same id with a different budget,
or its creatives could have been swapped. So every bound field is re-compared against LIVE
platform state at spend time:

    the token's subject matches this platform campaign
    the token's intent digest matches the campaign's recorded digest
    budget, currency, and flight dates match the live campaign
    the creatives on disk still hash to what was approved
    the intended spend is within the approved ceiling

AN EMPTY LIVE DIGEST REFUSES. It does not pass. A campaign carrying no digest is a campaign
this system cannot prove anything about, and "cannot prove" resolves to no.

ALL MISMATCHES ARE REPORTED TOGETHER. Raising on the first one makes the operator fix a
field, re-run, and discover the next one, which turns a five-field problem into five
review cycles. The refusal names every failure at once.

THE RESULT IS A CAPABILITY, NOT A BOOLEAN. `authorize` returns a `SpendGrant` that cannot
be constructed from outside this module. A boolean can be shadowed by a later `= True`; a
capability object that only this function can mint cannot. And the grant is frozen, because
mutating its campaign id after minting was a real hole in the design this distills:
authorize campaign A, retarget the grant at campaign B.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..approval.errors import ApprovalError
from ..approval.ledger import NonceLedger
from ..approval.tokens import ApprovalToken, verify_and_consume
from .campaign import Campaign
from .creative import CreativeRead
from .platform import CampaignState

_MINT = object()


class SpendRefused(Exception):
    """Every reason the gate said no, in one message."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("spend refused: " + "; ".join(reasons))


@dataclass(frozen=True)
class SpendGrant:
    """Proof that every gate passed. Mintable only by `authorize`."""

    platform_campaign_id: str
    intent_digest: str
    approved_ceiling_minor: int
    authorized_spend_minor: int
    approver: str

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if kwargs.pop("_mint", None) is not _MINT:
            raise TypeError(
                "SpendGrant cannot be constructed directly; it is minted only by "
                "spend_gate.authorize after every check has passed"
            )
        object.__setattr__(self, "platform_campaign_id", kwargs["platform_campaign_id"])
        object.__setattr__(self, "intent_digest", kwargs["intent_digest"])
        object.__setattr__(self, "approved_ceiling_minor", kwargs["approved_ceiling_minor"])
        object.__setattr__(self, "authorized_spend_minor", kwargs["authorized_spend_minor"])
        object.__setattr__(self, "approver", kwargs["approver"])


def authorize(
    *,
    campaign: Campaign,
    token: ApprovalToken,
    signature: str,
    key: bytes,
    ledger: NonceLedger,
    live_state: CampaignState,
    creative_reads: list[CreativeRead],
    expected_intent_digest: str,
    intended_spend_minor: int,
    expected_starts_at: str | None = None,
    expected_ends_at: str | None = None,
    now: float | None = None,
) -> SpendGrant:
    """Run every check. Raise `SpendRefused` naming all failures, or mint a grant.

    `expected_intent_digest` must be recomputed by the caller from the config it is about
    to act on. Reading the digest back off the token would verify nothing at all: the
    token would be checked against itself.
    """
    reasons: list[str] = []

    # 1. The signed token. Signature, expiry, scope, content binding, spend ceiling, and
    #    single use, in that order. This raises rather than accumulating, because a bad
    #    signature makes every other field in the token meaningless to report on.
    try:
        verified = verify_and_consume(
            token=token,
            signature=signature,
            key=key,
            expected_scope=f"spend:{campaign.campaign_id}",
            expected_content_hash=expected_intent_digest,
            ledger=ledger,
            intended_spend_minor=intended_spend_minor,
            now=now,
        )
    except ApprovalError as exc:
        raise SpendRefused([str(exc)]) from exc

    # 2. Re-check every bound field against LIVE platform state.
    if verified.subject != live_state.platform_campaign_id:
        reasons.append(
            f"approval names campaign {verified.subject!r}, live campaign is "
            f"{live_state.platform_campaign_id!r}"
        )
    if not live_state.intent_digest:
        reasons.append(
            "live campaign carries no intent digest; refusing rather than assuming it is "
            "the campaign that was approved"
        )
    elif live_state.intent_digest != expected_intent_digest:
        reasons.append(
            "live campaign's intent digest differs from the approved intent: the campaign "
            "on the platform is not the one that was reviewed"
        )
    g = campaign.guardrails
    if live_state.lifetime_budget_minor != g.lifetime_budget_minor:
        reasons.append(
            f"live lifetime budget {live_state.lifetime_budget_minor} does not match the "
            f"approved {g.lifetime_budget_minor}"
        )
    if live_state.currency != g.currency:
        reasons.append(
            f"live currency {live_state.currency!r} does not match approved {g.currency!r}"
        )

    # Flight dates. These are INSIDE the intent digest, so a mismatch here should already
    # have been caught by the digest comparison above. They are re-checked anyway because
    # the digest is a derived value and this is the direct observation: if the two ever
    # disagree, the direct check is the one to trust. A campaign silently re-flighted to
    # run for a month instead of a week spends the approved ceiling against an audience
    # and a moment nobody reviewed.
    if expected_starts_at is not None and live_state.starts_at != expected_starts_at:
        reasons.append(
            f"live flight start {live_state.starts_at!r} does not match the approved "
            f"{expected_starts_at!r}"
        )
    if expected_ends_at is not None and live_state.ends_at != expected_ends_at:
        reasons.append(
            f"live flight end {live_state.ends_at!r} does not match the approved "
            f"{expected_ends_at!r}"
        )

    # 3. The creatives must still be the approved bytes.
    bad = [f"{r.ref}: {r.error}" for r in creative_reads if not r.ok]
    if bad:
        reasons.append("creative(s) no longer match what was approved: " + "; ".join(bad))
    approved_hashes = {c.content_hash for c in campaign.creatives}
    read_hashes = {r.approved_hash for r in creative_reads}
    if approved_hashes != read_hashes:
        reasons.append(
            "the set of creatives read does not match the set in the approved campaign"
        )

    if reasons:
        raise SpendRefused(reasons)

    return SpendGrant(
        _mint=_MINT,
        platform_campaign_id=live_state.platform_campaign_id,
        intent_digest=expected_intent_digest,
        approved_ceiling_minor=int(verified.max_spend_minor or 0),
        authorized_spend_minor=intended_spend_minor,
        approver=verified.approver,
    )
