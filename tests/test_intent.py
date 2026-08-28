"""The intent digest: what an approval is actually bound to.

Determinism here is a double-spend control, not a convenience. A digest that varies by
machine, by directory, or by call means a timed-out retry cannot find the campaign the
first attempt may have created, so it creates a SECOND real one spending real money.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from conftest import CREATIVE_HASHES, make_campaign

from ocm.paid.campaign import CreativeRef
from ocm.paid.intent import canonical_instant, intent_digest, render_review_card

STARTS = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
ENDS = STARTS + timedelta(days=7)


def digest_of(campaign, *, starts=STARTS, ends=ENDS) -> str:
    return intent_digest(campaign, starts_at=starts, ends_at=ends)


# --------------------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------------------


def test_the_digest_is_deterministic_across_repeated_calls(guardrails):
    campaign = make_campaign(guardrails=guardrails)
    assert digest_of(campaign) == digest_of(campaign)


def test_the_digest_is_a_thirty_two_hex_character_string(guardrails):
    d = digest_of(make_campaign(guardrails=guardrails))
    assert len(d) == 32
    assert set(d) <= set("0123456789abcdef")


def test_the_digest_does_not_change_when_the_config_directory_changes(guardrails):
    """MACHINE INDEPENDENCE. The config's absolute directory differs between a laptop and
    a server, and between two checkouts on one laptop. Including it would make the same
    campaign digest differently from a different working directory, which defeats adoption
    and creates the duplicate campaign the determinism was supposed to prevent.
    """
    laptop = make_campaign(guardrails=guardrails, source_dir="/home/build-a/checkout/config")
    server = make_campaign(guardrails=guardrails, source_dir="/srv/deploy/current/config")
    assert laptop.source_dir != server.source_dir
    assert digest_of(laptop) == digest_of(server)


def test_reordering_the_creatives_does_not_change_the_digest(guardrails):
    """Sorted hashes, so shuffling the config list is correctly recognized as the same
    intent rather than as a new campaign to create."""
    a = CreativeRef(ref="a.txt", content_hash="a" * 64)
    b = CreativeRef(ref="b.txt", content_hash="b" * 64)
    forward = make_campaign(guardrails=guardrails, creatives=(a, b))
    reversed_ = make_campaign(guardrails=guardrails, creatives=(b, a))
    assert digest_of(forward) == digest_of(reversed_)


def test_renaming_a_creative_ref_does_not_change_the_digest(guardrails):
    """Creatives are bound by WHAT THEY ARE. Moving the same bytes to a new path is not a
    new campaign."""
    same_bytes_new_path = (
        CreativeRef(ref="renamed/a.txt", content_hash=CREATIVE_HASHES["a.txt"]),
        CreativeRef(ref="renamed/b.txt", content_hash=CREATIVE_HASHES["b.txt"]),
    )
    assert digest_of(make_campaign(guardrails=guardrails)) == digest_of(
        make_campaign(guardrails=guardrails, creatives=same_bytes_new_path)
    )


# --------------------------------------------------------------------------------------
# sensitivity: every field that changes what is bought
# --------------------------------------------------------------------------------------


def test_a_different_budget_changes_the_digest(guardrails):
    from ocm.paid.campaign import Guardrails

    bigger = Guardrails(
        lifetime_budget_minor=guardrails.lifetime_budget_minor + 1,
        currency=guardrails.currency,
        decision_metric=guardrails.decision_metric,
    )
    assert digest_of(make_campaign(guardrails=guardrails)) != digest_of(
        make_campaign(guardrails=bigger)
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("landing_url", "https://example.invalid/a-different-page"),
        ("campaign_id", "camp-2"),
        ("geo", ("GB",)),
    ],
)
def test_changing_a_material_field_changes_the_digest(guardrails, field, value):
    base = make_campaign(guardrails=guardrails)
    changed = make_campaign(guardrails=guardrails, **{field: value})
    assert digest_of(base) != digest_of(changed)


def test_changing_a_creative_content_hash_changes_the_digest(guardrails):
    """The creative is the thing the human looked at. Swapping it must invalidate the
    approval, not quietly ride along on it."""
    swapped = (
        CreativeRef(ref="a.txt", content_hash="a" * 64),
        CreativeRef(ref="b.txt", content_hash="c" * 64),
    )
    assert digest_of(make_campaign(guardrails=guardrails)) != digest_of(
        make_campaign(guardrails=guardrails, creatives=swapped)
    )


def test_changing_the_currency_or_the_decision_metric_changes_the_digest(guardrails):
    from ocm.paid.campaign import Guardrails

    base = make_campaign(guardrails=guardrails)
    other_currency = Guardrails(
        lifetime_budget_minor=guardrails.lifetime_budget_minor,
        currency="EUR",
        decision_metric=guardrails.decision_metric,
    )
    other_metric = Guardrails(
        lifetime_budget_minor=guardrails.lifetime_budget_minor,
        currency=guardrails.currency,
        decision_metric="roas",
    )
    assert digest_of(base) != digest_of(make_campaign(guardrails=other_currency))
    assert digest_of(base) != digest_of(make_campaign(guardrails=other_metric))


def test_changing_the_flight_dates_changes_the_digest(guardrails):
    campaign = make_campaign(guardrails=guardrails)
    assert digest_of(campaign) != digest_of(campaign, starts=STARTS + timedelta(days=1))
    assert digest_of(campaign) != digest_of(campaign, ends=ENDS + timedelta(days=1))


def test_ends_at_must_be_after_starts_at(guardrails):
    campaign = make_campaign(guardrails=guardrails)
    with pytest.raises(ValueError, match="ends_at must be after starts_at"):
        digest_of(campaign, ends=STARTS)
    with pytest.raises(ValueError, match="ends_at must be after starts_at"):
        digest_of(campaign, ends=STARTS - timedelta(hours=1))


# --------------------------------------------------------------------------------------
# canonical instants
# --------------------------------------------------------------------------------------


def test_canonical_instant_refuses_a_naive_datetime():
    """Two hosts would disagree on what a naive instant means, and each would compute a
    different digest for the same campaign."""
    with pytest.raises(ValueError, match="timezone-naive"):
        canonical_instant(datetime(2026, 3, 1, 9, 0))


def test_canonical_instant_truncates_seconds_and_microseconds():
    """Two hosts with slightly different clocks must agree on the digest of one intent."""
    assert canonical_instant(
        datetime(2026, 3, 1, 9, 0, 59, 999_999, tzinfo=UTC)
    ) == "2026-03-01T09:00Z"


def test_two_equal_instants_in_different_timezones_canonicalize_identically():
    utc = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
    tokyo = datetime(2026, 3, 1, 23, 0, tzinfo=timezone(timedelta(hours=9)))
    new_york = datetime(2026, 3, 1, 9, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert canonical_instant(utc) == canonical_instant(tokyo) == canonical_instant(new_york)


def test_the_digest_is_unchanged_by_the_timezone_the_flight_was_expressed_in(guardrails):
    campaign = make_campaign(guardrails=guardrails)
    tokyo_start = STARTS.astimezone(timezone(timedelta(hours=9)))
    tokyo_end = ENDS.astimezone(timezone(timedelta(hours=9)))
    assert digest_of(campaign) == digest_of(campaign, starts=tokyo_start, ends=tokyo_end)


def test_sub_minute_clock_skew_does_not_change_the_digest(guardrails):
    campaign = make_campaign(guardrails=guardrails)
    skewed = STARTS.replace(second=41, microsecond=7)
    assert digest_of(campaign) == digest_of(campaign, starts=skewed)


# --------------------------------------------------------------------------------------
# the review card
# --------------------------------------------------------------------------------------


def test_the_review_card_prints_every_bound_field_and_the_digest(guardrails):
    campaign = make_campaign(guardrails=guardrails)
    digest = digest_of(campaign)
    card = render_review_card(campaign, starts_at=STARTS, ends_at=ENDS, digest=digest)

    assert campaign.campaign_id in card
    assert campaign.landing_url in card
    assert str(guardrails.lifetime_budget_minor) in card
    assert guardrails.currency in card
    assert guardrails.decision_metric in card
    assert "2026-03-01T09:00Z" in card
    assert digest in card


def test_the_review_card_prints_the_approved_hash_not_a_freshly_computed_one(guardrails):
    """Printing a hash recomputed at render time would show the reviewer the bytes
    currently on disk while the approval binds the bytes that were approved."""
    campaign = make_campaign(guardrails=guardrails)
    card = render_review_card(campaign, starts_at=STARTS, ends_at=ENDS, digest=digest_of(campaign))
    for creative in campaign.creatives:
        assert f"approved hash {creative.content_hash}" in card


def test_the_review_card_says_the_digest_is_the_binding(guardrails):
    campaign = make_campaign(guardrails=guardrails)
    card = render_review_card(campaign, starts_at=STARTS, ends_at=ENDS, digest=digest_of(campaign))
    assert "APPROVAL REQUIRED" in card
    assert "binds your decision to the digest" in card


def test_the_review_card_is_presentation_only_and_never_feeds_the_digest(guardrails):
    """The card is what the human READ; it is not what they approved. Re-rendering it can
    never reproduce the same bytes, so a digest of it could not be re-derived at spend
    time. Changing only the card must not move the digest.
    """
    campaign = make_campaign(guardrails=guardrails)
    before = digest_of(campaign)
    render_review_card(campaign, starts_at=STARTS, ends_at=ENDS, digest="deadbeef")
    assert digest_of(campaign) == before
