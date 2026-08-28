"""The gate between a human's approval and money moving.

A valid signature is where this check STARTS, not where it ends. Between approval and
spend the campaign could have been deleted and recreated under the same id with a different
budget, or its creatives swapped, so every bound field is re-compared against LIVE state.
"""

from __future__ import annotations

import dataclasses

import pytest
from conftest import CREATIVE_PAYLOADS, make_campaign

from ocm.approval.ledger import InMemoryLedger
from ocm.approval.tokens import issue
from ocm.paid.campaign import CreativeRef
from ocm.paid.creative import CreativeRead
from ocm.paid.platform import CampaignState
from ocm.paid.spend_gate import SpendGrant, SpendRefused, authorize

KEY = b"a-fixed-test-key-that-is-not-a-real-secret"
T0 = 1_800_000_000.0
DIGEST = "d" * 32
PLATFORM_ID = "dryrun-camp-000000000000"


@pytest.fixture
def ledger() -> InMemoryLedger:
    return InMemoryLedger()


@pytest.fixture
def campaign(guardrails):
    return make_campaign(guardrails=guardrails)


@pytest.fixture
def live_state(guardrails) -> CampaignState:
    return CampaignState(
        platform_campaign_id=PLATFORM_ID,
        status="PAUSED",
        intent_digest=DIGEST,
        lifetime_budget_minor=guardrails.lifetime_budget_minor,
        currency=guardrails.currency,
        starts_at="2026-03-01T09:00:00+00:00",
        ends_at="2026-03-08T09:00:00+00:00",
    )


@pytest.fixture
def creative_reads(campaign) -> list[CreativeRead]:
    """Reads whose payloads genuinely hash to their declared approved hash.

    They have to, because the gate re-hashes the payload rather than trusting the read's
    own `ok` flag: a CreativeRead is an ordinary object a caller constructs, so believing
    its self-report would make the last line of defense depend on the honesty of the thing
    it defends against.
    """
    return [
        CreativeRead(c.ref, c.content_hash, None, None, CREATIVE_PAYLOADS[c.ref])
        for c in campaign.creatives
    ]


@pytest.fixture
def token_and_sig(campaign, guardrails):
    return issue(
        scope=f"spend:{campaign.campaign_id}",
        content_hash=DIGEST,
        subject=PLATFORM_ID,
        key=KEY,
        max_spend_minor=guardrails.lifetime_budget_minor,
        approver="test-operator",
        now=T0,
    )


def run(campaign, token_and_sig, ledger, live_state, creative_reads, **overrides):
    token, sig = token_and_sig
    kwargs = dict(
        campaign=campaign,
        token=token,
        signature=sig,
        key=KEY,
        ledger=ledger,
        live_state=live_state,
        creative_reads=creative_reads,
        expected_intent_digest=DIGEST,
        intended_spend_minor=campaign.guardrails.lifetime_budget_minor,
        now=T0 + 1,
    )
    kwargs.update(overrides)
    return authorize(**kwargs)


# --------------------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------------------


def test_a_fully_consistent_request_mints_a_spend_grant(
    campaign, token_and_sig, ledger, live_state, creative_reads, guardrails
):
    grant = run(campaign, token_and_sig, ledger, live_state, creative_reads)

    assert isinstance(grant, SpendGrant)
    assert grant.platform_campaign_id == PLATFORM_ID
    assert grant.intent_digest == DIGEST
    assert grant.approved_ceiling_minor == guardrails.lifetime_budget_minor
    assert grant.authorized_spend_minor == guardrails.lifetime_budget_minor
    assert grant.approver == "test-operator"


def test_the_approval_is_single_use_even_when_everything_still_matches(
    campaign, token_and_sig, ledger, live_state, creative_reads
):
    run(campaign, token_and_sig, ledger, live_state, creative_reads)
    with pytest.raises(SpendRefused, match="already consumed"):
        run(campaign, token_and_sig, ledger, live_state, creative_reads)


# --------------------------------------------------------------------------------------
# the grant is a capability, not a boolean
# --------------------------------------------------------------------------------------


def test_a_spend_grant_cannot_be_constructed_directly():
    """A boolean can be shadowed by a later `= True`. A capability object that only
    `authorize` can mint cannot."""
    with pytest.raises(TypeError, match="minted only by"):
        SpendGrant(
            platform_campaign_id=PLATFORM_ID,
            intent_digest=DIGEST,
            approved_ceiling_minor=25_000,
            authorized_spend_minor=25_000,
            approver="me",
        )


def test_a_spend_grant_cannot_be_constructed_positionally_either():
    with pytest.raises(TypeError):
        SpendGrant(PLATFORM_ID, DIGEST, 25_000, 25_000, "me")


def test_a_spend_grant_cannot_be_retargeted_at_another_campaign(
    campaign, token_and_sig, ledger, live_state, creative_reads
):
    """Frozen, because mutating the campaign id after minting was a real hole in the
    design this distills: authorize campaign A, then point the grant at campaign B."""
    grant = run(campaign, token_and_sig, ledger, live_state, creative_reads)

    with pytest.raises(dataclasses.FrozenInstanceError):
        grant.platform_campaign_id = "some-other-campaign"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        grant.authorized_spend_minor = 10**9  # type: ignore[misc]

    assert grant.platform_campaign_id == PLATFORM_ID


# --------------------------------------------------------------------------------------
# live-state re-checks
# --------------------------------------------------------------------------------------


def test_a_live_budget_that_differs_from_the_approved_one_refuses(
    campaign, token_and_sig, ledger, live_state, creative_reads
):
    """The campaign could have been deleted and recreated under the same id with a
    different budget between approval and spend."""
    changed = dataclasses.replace(live_state, lifetime_budget_minor=99_999)
    with pytest.raises(SpendRefused, match="live lifetime budget"):
        run(campaign, token_and_sig, ledger, live_state=changed, creative_reads=creative_reads)


def test_a_live_currency_that_differs_from_the_approved_one_refuses(
    campaign, token_and_sig, ledger, live_state, creative_reads
):
    changed = dataclasses.replace(live_state, currency="EUR")
    with pytest.raises(SpendRefused, match="live currency"):
        run(campaign, token_and_sig, ledger, live_state=changed, creative_reads=creative_reads)


def test_an_empty_live_intent_digest_refuses_rather_than_passing(
    campaign, token_and_sig, ledger, live_state, creative_reads
):
    """"Cannot prove" resolves to no. A campaign carrying no digest is a campaign this
    system cannot prove anything about, and an emptiness check that treated "" as
    "matches everything" would wave through exactly the unknown campaign it is for.
    """
    blank = dataclasses.replace(live_state, intent_digest="")
    with pytest.raises(SpendRefused) as exc:
        run(campaign, token_and_sig, ledger, live_state=blank, creative_reads=creative_reads)
    assert any("carries no intent digest" in r for r in exc.value.reasons)


def test_a_live_digest_different_from_the_approved_one_refuses(
    campaign, token_and_sig, ledger, live_state, creative_reads
):
    other = dataclasses.replace(live_state, intent_digest="f" * 32)
    with pytest.raises(SpendRefused) as exc:
        run(campaign, token_and_sig, ledger, live_state=other, creative_reads=creative_reads)
    assert any("not the one that was reviewed" in r for r in exc.value.reasons)


def test_a_subject_naming_a_different_campaign_refuses(
    campaign, guardrails, ledger, live_state, creative_reads
):
    """The token names WHICH campaign it authorizes; presenting it against another one is
    a retarget attempt however innocent its cause."""
    token, sig = issue(
        scope=f"spend:{campaign.campaign_id}",
        content_hash=DIGEST,
        subject="a-completely-different-campaign",
        key=KEY,
        max_spend_minor=guardrails.lifetime_budget_minor,
        now=T0,
    )
    with pytest.raises(SpendRefused) as exc:
        run(campaign, (token, sig), ledger, live_state, creative_reads)
    assert any("approval names campaign" in r for r in exc.value.reasons)


def test_a_creative_that_no_longer_matches_refuses(
    campaign, token_and_sig, ledger, live_state, creative_reads
):
    broken = list(creative_reads)
    broken[0] = CreativeRead(
        broken[0].ref, broken[0].approved_hash, None, None, None, "content hash mismatch"
    )
    with pytest.raises(SpendRefused) as exc:
        run(campaign, token_and_sig, ledger, live_state, creative_reads=broken)
    assert any("no longer match what was approved" in r for r in exc.value.reasons)


def test_a_creative_set_that_differs_from_the_approved_set_refuses(
    campaign, token_and_sig, ledger, live_state
):
    """Substituting one approved asset for another approved-looking one is still an
    unreviewed campaign."""
    swapped = [CreativeRead("z.txt", "z" * 64, None, None, b"bytes")]
    with pytest.raises(SpendRefused) as exc:
        run(campaign, token_and_sig, ledger, live_state, creative_reads=swapped)
    assert any("do not match, one for one" in r for r in exc.value.reasons)


# --------------------------------------------------------------------------------------
# every reason at once
# --------------------------------------------------------------------------------------


def test_all_mismatches_are_reported_together(campaign, guardrails, ledger, creative_reads):
    """Raising on the first one makes the operator fix a field, re-run, and discover the
    next one, turning a five-field problem into five review cycles.
    """
    token, sig = issue(
        scope=f"spend:{campaign.campaign_id}",
        content_hash=DIGEST,
        subject="a-different-campaign",  # problem 1: subject mismatch
        key=KEY,
        max_spend_minor=guardrails.lifetime_budget_minor,
        now=T0,
    )
    broken_state = CampaignState(
        platform_campaign_id=PLATFORM_ID,
        status="PAUSED",
        intent_digest=DIGEST,
        lifetime_budget_minor=99_999,  # problem 2: budget
        currency="EUR",  # problem 3: currency
        starts_at="x",
        ends_at="y",
    )
    broken_reads = [
        CreativeRead(r.ref, r.approved_hash, None, None, None, "content hash mismatch")
        for r in creative_reads
    ]  # problem 4: creatives

    with pytest.raises(SpendRefused) as exc:
        run(
            campaign,
            (token, sig),
            ledger,
            live_state=broken_state,
            creative_reads=broken_reads,
        )

    assert len(exc.value.reasons) >= 3
    # And the single-line message names them all, because that is what an operator reads.
    for reason in exc.value.reasons:
        assert reason in str(exc.value)


# --------------------------------------------------------------------------------------
# the expected digest is the caller's, not the token's
# --------------------------------------------------------------------------------------


def test_a_changed_expected_digest_refuses(
    campaign, token_and_sig, ledger, live_state, creative_reads
):
    """`expected_intent_digest` must be recomputed by the caller from the config it is
    about to act on. Reading the digest back off the token would check the token against
    itself and verify nothing at all, so this test is what proves the parameter is load
    bearing rather than decorative.
    """
    with pytest.raises(SpendRefused) as exc:
        run(
            campaign,
            token_and_sig,
            ledger,
            live_state,
            creative_reads,
            expected_intent_digest="0" * 32,
        )
    assert any("content changed after approval" in r for r in exc.value.reasons)


def test_a_token_refused_on_the_expected_digest_does_not_burn_its_nonce(
    campaign, token_and_sig, ledger, live_state, creative_reads
):
    """So a caller that recomputed the digest from the wrong config can fix it and retry
    with the same human approval."""
    with pytest.raises(SpendRefused):
        run(
            campaign, token_and_sig, ledger, live_state, creative_reads,
            expected_intent_digest="0" * 32,
        )
    assert ledger.seen(token_and_sig[0].nonce) is False
    assert run(campaign, token_and_sig, ledger, live_state, creative_reads)


# --------------------------------------------------------------------------------------
# the token-level refusals still short-circuit
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides,why",
    [
        ({"key": b"the-wrong-key-entirely"}, "bad signature"),
        ({"now": T0 + 10_000_000}, "expired"),
        ({"intended_spend_minor": 10**9}, "over the ceiling"),
    ],
)
def test_a_bad_token_refuses_with_a_single_reason(
    campaign, token_and_sig, ledger, live_state, creative_reads, overrides, why
):
    """A bad signature makes every other field in the token meaningless to report on, so
    the token checks raise rather than accumulating alongside live-state findings."""
    with pytest.raises(SpendRefused) as exc:
        run(campaign, token_and_sig, ledger, live_state, creative_reads, **overrides)
    assert len(exc.value.reasons) == 1, why


def test_a_publish_scoped_token_cannot_authorize_a_spend(
    campaign, guardrails, ledger, live_state, creative_reads
):
    token, sig = issue(
        scope=f"publish:{campaign.campaign_id}",
        content_hash=DIGEST,
        subject=PLATFORM_ID,
        key=KEY,
        max_spend_minor=guardrails.lifetime_budget_minor,
        now=T0,
    )
    with pytest.raises(SpendRefused, match="does not authorize"):
        run(campaign, (token, sig), ledger, live_state, creative_reads)


def test_spend_refused_carries_its_reasons_as_a_list(
    campaign, token_and_sig, ledger, live_state, creative_reads
):
    """Callers report them individually; a pre-joined string would force re-parsing."""
    with pytest.raises(SpendRefused) as exc:
        run(
            campaign, token_and_sig, ledger,
            live_state=dataclasses.replace(live_state, currency="EUR"),
            creative_reads=creative_reads,
        )
    assert isinstance(exc.value.reasons, list)
    assert all(isinstance(r, str) for r in exc.value.reasons)


def test_the_gate_needs_no_creative_bytes_to_refuse_a_mismatched_set(
    campaign, token_and_sig, ledger, live_state
):
    """A refusal must be reachable without having read anything, so a hostile or missing
    asset cannot make the gate fail open by making it fail early."""
    with pytest.raises(SpendRefused):
        run(campaign, token_and_sig, ledger, live_state, creative_reads=[])


def test_a_campaign_creative_set_and_read_set_must_agree_by_hash_not_by_count(
    campaign, token_and_sig, ledger, live_state
):
    right_count_wrong_assets = [
        CreativeRead(c.ref, "9" * 64, None, None, b"bytes") for c in campaign.creatives
    ]
    with pytest.raises(SpendRefused) as exc:
        run(campaign, token_and_sig, ledger, live_state, creative_reads=right_count_wrong_assets)
    assert any("do not match, one for one" in r for r in exc.value.reasons)


def test_a_reordered_creative_read_list_is_still_accepted(
    campaign, token_and_sig, ledger, live_state, creative_reads
):
    """Sets, not sequences: the order a directory listing came back in is not a change to
    what was approved."""
    assert run(
        campaign, token_and_sig, ledger, live_state, creative_reads=list(reversed(creative_reads))
    )


def test_the_campaign_creatives_used_for_the_set_check_come_from_the_campaign(guardrails):
    """Documents which side of the comparison is authoritative."""
    campaign = make_campaign(
        guardrails=guardrails,
        creatives=(CreativeRef(ref="only.txt", content_hash="a" * 64),),
    )
    assert {c.content_hash for c in campaign.creatives} == {"a" * 64}


# --------------------------------------------------------------------------------------
# flight dates are bound too
# --------------------------------------------------------------------------------------


def test_a_reflighted_campaign_is_refused(campaign, token_and_sig, ledger, live_state, creative_reads):
    """Flight dates are inside the intent digest, so they are re-checked directly too.

    A campaign silently re-flighted to run for a month instead of a week spends the whole
    approved ceiling against an audience and a moment nobody reviewed. The digest check
    would normally catch it, but the digest is a derived value and this is the direct
    observation of the field itself: if the two ever disagree, this is the one to trust.
    """
    with pytest.raises(SpendRefused) as exc:
        run(
            campaign,
            token_and_sig,
            ledger,
            live_state,
            creative_reads,
            expected_starts_at="2099-01-01T00:00:00+00:00",
        )
    assert any("flight start" in r for r in exc.value.reasons)


def test_a_shortened_or_extended_end_date_is_refused(
    campaign, token_and_sig, ledger, live_state, creative_reads
):
    with pytest.raises(SpendRefused) as exc:
        run(
            campaign,
            token_and_sig,
            ledger,
            live_state,
            creative_reads,
            expected_ends_at="2099-12-31T00:00:00+00:00",
        )
    assert any("flight end" in r for r in exc.value.reasons)


def test_a_matching_flight_window_passes(
    campaign, token_and_sig, ledger, live_state, creative_reads
):
    grant = run(
        campaign,
        token_and_sig,
        ledger,
        live_state,
        creative_reads,
        expected_starts_at=live_state.starts_at,
        expected_ends_at=live_state.ends_at,
    )
    assert grant.platform_campaign_id == PLATFORM_ID


def test_both_flight_mismatches_are_reported_together(
    campaign, token_and_sig, ledger, live_state, creative_reads
):
    """All reasons at once. Reporting one at a time turns a two-field problem into two
    review cycles."""
    with pytest.raises(SpendRefused) as exc:
        run(
            campaign,
            token_and_sig,
            ledger,
            live_state,
            creative_reads,
            expected_starts_at="2099-01-01T00:00:00+00:00",
            expected_ends_at="2099-12-31T00:00:00+00:00",
        )
    assert len(exc.value.reasons) >= 2


# --------------------------------------------------------------------------------------
# hardening found by adversarial review
# --------------------------------------------------------------------------------------


def test_a_live_campaign_that_is_not_paused_refuses(
    campaign, token_and_sig, ledger, live_state, creative_reads
):
    """A campaign already delivering is not one awaiting authorization to spend.

    Authorizing it rubber-stamps money that is already moving. This codebase has no way to
    leave PAUSED, so reaching this state means something changed the campaign outside the
    system, which is precisely when the gate should refuse.
    """
    active = dataclasses.replace(live_state, status="ACTIVE")
    with pytest.raises(SpendRefused) as exc:
        run(campaign, token_and_sig, ledger, active, creative_reads)
    assert any("not 'PAUSED'" in r for r in exc.value.reasons)


def test_a_fabricated_creative_read_is_refused(
    campaign, token_and_sig, ledger, live_state
):
    """The gate re-hashes the payload rather than trusting the read's own report.

    A CreativeRead is an ordinary object a caller constructs. Believing its `ok` flag would
    make the last line of defense depend on the honesty of the thing it defends against.
    """
    lying = [
        CreativeRead(c.ref, c.content_hash, None, None, b"completely different bytes")
        for c in campaign.creatives
    ]
    with pytest.raises(SpendRefused) as exc:
        run(campaign, token_and_sig, ledger, live_state, lying)
    assert any("do not hash to the approved value" in r for r in exc.value.reasons)


def test_one_asset_presented_twice_cannot_satisfy_a_two_asset_approval(
    campaign, token_and_sig, ledger, live_state
):
    """Compared ref by ref, not as sets. A set comparison collapses duplicates."""
    first = campaign.creatives[0]
    doubled = [
        CreativeRead(first.ref, first.content_hash, None, None, CREATIVE_PAYLOADS[first.ref])
    ] * 2
    with pytest.raises(SpendRefused) as exc:
        run(campaign, token_and_sig, ledger, live_state, doubled)
    assert any("one for one" in r for r in exc.value.reasons)


def test_a_zero_or_negative_intended_spend_is_refused(
    campaign, token_and_sig, ledger, live_state, creative_reads
):
    for amount in (0, -1):
        with pytest.raises(SpendRefused):
            run(
                campaign,
                token_and_sig,
                InMemoryLedger(),
                live_state,
                creative_reads,
                intended_spend_minor=amount,
            )


def test_a_live_state_mismatch_does_not_burn_the_approval(
    campaign, token_and_sig, ledger, live_state, creative_reads
):
    """THE ORDERING GUARANTEE, on the spend side.

    The nonce is consumed only once the gate is certain it will return a grant. A transient
    platform mismatch must not destroy a valid human approval and force a person to issue a
    new one. Previously the nonce was burned during verification, before any live check ran.
    """
    token, _ = token_and_sig
    mismatched = dataclasses.replace(live_state, currency="EUR")

    with pytest.raises(SpendRefused):
        run(campaign, token_and_sig, ledger, mismatched, creative_reads)
    assert not ledger.seen(token.nonce), "a refused spend burned the approval"

    # The very same approval still works once the platform state is correct again.
    grant = run(campaign, token_and_sig, ledger, live_state, creative_reads)
    assert grant.platform_campaign_id == PLATFORM_ID
    assert ledger.seen(token.nonce), "a granted spend must burn the approval"
