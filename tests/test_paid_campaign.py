"""Paid campaign config: guardrails, the closed key allowlist, and the stop clock."""

from __future__ import annotations

import dataclasses

import pytest
from conftest import make_campaign

from ocm.paid.campaign import Campaign, CreativeRef, Guardrails, StopClock

BASE_GUARDRAILS = {
    "lifetime_budget_minor": 25_000,
    "currency": "USD",
    "decision_metric": "cost_per_purchase",
}


def guardrails_with(**overrides) -> dict:
    cfg = dict(BASE_GUARDRAILS)
    cfg.update(overrides)
    return cfg


# --------------------------------------------------------------------------------------
# guardrails
# --------------------------------------------------------------------------------------


def test_guardrails_accept_a_well_formed_config():
    g = Guardrails.from_config(guardrails_with())
    assert g.lifetime_budget_minor == 25_000
    assert g.currency == "USD"
    assert g.decision_metric == "cost_per_purchase"
    assert g.max_active_campaigns == 1
    assert g.max_collection_failures == 3


@pytest.mark.parametrize("budget", [250.0, 250.5, 0.0])
def test_guardrails_reject_a_float_budget(budget):
    """Float arithmetic on money produces a ceiling that is occasionally a hundredth over,
    and "occasionally over the approved amount" is the one thing a spend guard may not be.
    """
    with pytest.raises(ValueError, match="integer in minor units"):
        Guardrails.from_config(guardrails_with(lifetime_budget_minor=budget))


@pytest.mark.parametrize("budget", [0, -1, -25_000])
def test_guardrails_reject_a_non_positive_budget(budget):
    with pytest.raises(ValueError, match="must be positive"):
        Guardrails.from_config(guardrails_with(lifetime_budget_minor=budget))


@pytest.mark.parametrize("currency", ["usd", "US", "USDD", "US1", "", "  USD"])
def test_guardrails_reject_a_malformed_currency_code(currency):
    """A currency that is not an ISO code makes every stored amount ambiguous."""
    with pytest.raises(ValueError, match="ISO-4217"):
        Guardrails.from_config(guardrails_with(currency=currency))


@pytest.mark.parametrize("metric", ["", "   ", "\n"])
def test_guardrails_reject_an_empty_decision_metric(metric):
    """Pre-registration: choosing the metric afterwards from the results is how every
    campaign becomes a success, because there is always some number that went up."""
    with pytest.raises(ValueError, match="decision_metric is required"):
        Guardrails.from_config(guardrails_with(decision_metric=metric))


def test_guardrails_require_an_account_ceiling_once_more_than_one_campaign_may_run():
    """N campaigns each at the per-campaign cap is N times the exposure, and the
    per-campaign figure reads like a total."""
    with pytest.raises(ValueError, match="account_ceiling_minor is required"):
        Guardrails.from_config(guardrails_with(max_active_campaigns=3))


def test_guardrails_accept_multiple_campaigns_once_an_account_ceiling_is_present():
    g = Guardrails.from_config(
        guardrails_with(max_active_campaigns=3, account_ceiling_minor=100_000)
    )
    assert (g.max_active_campaigns, g.account_ceiling_minor) == (3, 100_000)


def test_guardrails_reject_a_negative_campaign_count():
    with pytest.raises(ValueError, match="non-negative"):
        Guardrails.from_config(guardrails_with(max_active_campaigns=-1))


def test_an_unknown_guardrail_key_is_an_error_not_a_silent_default():
    """A typo'd key that is ignored gives you a campaign running with a default nobody
    chose, and there is no error anywhere to find it by."""
    with pytest.raises(ValueError, match="unknown guardrail key"):
        Guardrails.from_config(guardrails_with(lifetime_budget_minior=25_000))


def test_a_missing_required_guardrail_key_raises_rather_than_defaulting():
    with pytest.raises(KeyError):
        Guardrails.from_config({"currency": "USD", "decision_metric": "cpa"})


# --------------------------------------------------------------------------------------
# creative refs
# --------------------------------------------------------------------------------------


def test_creative_ref_requires_a_content_hash():
    """A ref with no hash binds to whatever is at that path at read time, which is exactly
    the binding the approval exists to prevent."""
    with pytest.raises(ValueError, match="content_hash is required"):
        CreativeRef(ref="a.txt", content_hash="")


def test_creative_ref_requires_a_ref():
    with pytest.raises(ValueError, match="ref must not be empty"):
        CreativeRef(ref="", content_hash="a" * 64)


def test_creative_ref_is_frozen():
    ref = CreativeRef(ref="a.txt", content_hash="a" * 64)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.content_hash = "b" * 64  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# campaigns
# --------------------------------------------------------------------------------------


def campaign_cfg(**overrides) -> dict:
    cfg = {
        "campaign_id": "camp-1",
        "objective": "conversions",
        "optimization_event": "purchase",
        "landing_url": "https://example.invalid/p",
        "run_days": 7,
        "geo": ["US"],
        "languages": ["en"],
        "creatives": [
            {"ref": "a.txt", "content_hash": "a" * 64},
            {"ref": "b.txt", "content_hash": "b" * 64},
        ],
        "guardrails": guardrails_with(),
    }
    cfg.update(overrides)
    return cfg


def test_campaign_loads_from_config_and_keeps_the_source_directory():
    c = Campaign.from_config(campaign_cfg(), source_dir="/abs/config/dir")
    assert c.campaign_id == "camp-1"
    assert c.source_dir == "/abs/config/dir"
    assert len(c.creatives) == 2


def test_campaign_with_no_creatives_cannot_be_approved():
    with pytest.raises(ValueError, match="no creatives"):
        Campaign.from_config(campaign_cfg(creatives=[]))


def test_campaign_rejects_duplicate_creative_refs():
    """Two entries for the same asset means the review card shows a count that is not the
    count of distinct assets."""
    with pytest.raises(ValueError, match="duplicate creative refs"):
        Campaign.from_config(
            campaign_cfg(
                creatives=[
                    {"ref": "a.txt", "content_hash": "a" * 64},
                    {"ref": "a.txt", "content_hash": "b" * 64},
                ]
            )
        )


@pytest.mark.parametrize("geo", ["us", "USA", "U", "U1", "united states"])
def test_campaign_rejects_a_malformed_geo_code(geo):
    """A geo the platform does not recognize silently becomes worldwide targeting."""
    with pytest.raises(ValueError, match="ISO-3166-1"):
        Campaign.from_config(campaign_cfg(geo=[geo]))


@pytest.mark.parametrize("run_days", [0, -1])
def test_campaign_rejects_non_positive_run_days(run_days):
    with pytest.raises(ValueError, match="run_days must be positive"):
        Campaign.from_config(campaign_cfg(run_days=run_days))


@pytest.mark.parametrize("campaign_id", ["", "   "])
def test_campaign_requires_a_campaign_id(campaign_id):
    with pytest.raises(ValueError, match="campaign_id is required"):
        Campaign.from_config(campaign_cfg(campaign_id=campaign_id))


def test_an_unknown_campaign_key_is_an_error():
    with pytest.raises(ValueError, match="unknown campaign key"):
        Campaign.from_config(campaign_cfg(daily_budget_minor=500))


def test_campaign_carries_no_start_date():
    """The start instant is decided once, at review-card time, so it appears on the
    document the human actually reads rather than being computed later by whichever
    process happened to run."""
    fields = {f for f in Campaign.__dataclass_fields__}
    assert not {"starts_at", "start_date", "start"} & fields


def test_the_shipped_campaign_config_loads():
    from conftest import CONFIG_DIR

    from ocm import config as cfgmod

    conf = cfgmod.load(CONFIG_DIR / "campaign.toml")
    campaign = Campaign.from_config(conf.data, source_dir=conf.source_dir)
    assert campaign.guardrails.lifetime_budget_minor == 25_000
    assert campaign.guardrails.decision_metric
    assert len(campaign.creatives) == 2


# --------------------------------------------------------------------------------------
# the stop clock
# --------------------------------------------------------------------------------------


@pytest.fixture
def gr() -> Guardrails:
    return Guardrails(
        lifetime_budget_minor=10_000,
        currency="USD",
        decision_metric="cpa",
        max_collection_failures=2,
    )


def test_nothing_stops_a_healthy_round(gr):
    clock = StopClock(guardrails=gr)
    assert clock.check(spent_minor=100, collection_failed=False) is None
    assert clock.stopped is None


def test_budget_stops_the_round_at_the_ceiling_not_past_it(gr):
    clock = StopClock(guardrails=gr)
    assert clock.check(spent_minor=9_999, collection_failed=False) is None
    assert StopClock(guardrails=gr).check(spent_minor=10_000, collection_failed=False) == "budget"


def test_consecutive_unreadable_collections_stop_the_round(gr):
    """A campaign whose results cannot be read is a campaign spending blind."""
    clock = StopClock(guardrails=gr)
    assert clock.check(spent_minor=0, collection_failed=True) is None
    assert clock.check(spent_minor=0, collection_failed=True) == "unreadable_state"


def test_the_failure_counter_resets_on_a_successful_collection(gr):
    """Consecutive, not cumulative: an intermittent blip must not eventually stop a
    perfectly healthy campaign."""
    clock = StopClock(guardrails=gr)
    clock.check(spent_minor=0, collection_failed=True)
    clock.check(spent_minor=0, collection_failed=False)
    assert clock.consecutive_collection_failures == 0
    assert clock.check(spent_minor=0, collection_failed=True) is None


def test_a_collateral_signal_stops_the_round(gr):
    clock = StopClock(guardrails=gr)
    reason = clock.check(spent_minor=0, collection_failed=False, collateral_signal=True)
    assert reason == "collateral"


def test_budget_takes_precedence_when_every_trigger_is_true_at_once(gr):
    """Precedence-ordered so the reported reason is stable across runs rather than
    depending on dict iteration order."""
    clock = StopClock(guardrails=gr, consecutive_collection_failures=1)
    reason = clock.check(spent_minor=10_000, collection_failed=True, collateral_signal=True)
    assert reason == "budget"


def test_unreadable_state_takes_precedence_over_collateral(gr):
    clock = StopClock(guardrails=gr, consecutive_collection_failures=1)
    reason = clock.check(spent_minor=0, collection_failed=True, collateral_signal=True)
    assert reason == "unreadable_state"


def test_the_declared_precedence_order_matches_the_observed_one(gr):
    assert StopClock.TRIGGERS == ("budget", "unreadable_state", "collateral")


def test_the_clock_stays_latched_once_it_has_stopped(gr):
    """A stopping rule that un-stops itself when the next reading looks better is not a
    stopping rule."""
    clock = StopClock(guardrails=gr)
    assert clock.check(spent_minor=10_000, collection_failed=False) == "budget"

    for _ in range(3):
        again = clock.check(spent_minor=0, collection_failed=False, collateral_signal=False)
        assert again == "budget"
    assert clock.stopped == "budget"


def test_a_latched_clock_does_not_keep_advancing_its_failure_counter(gr):
    """Once stopped, `check` returns early, so nothing downstream reads a counter that has
    kept climbing after the decision was already made."""
    clock = StopClock(guardrails=gr)
    clock.check(spent_minor=10_000, collection_failed=False)
    before = clock.consecutive_collection_failures
    clock.check(spent_minor=0, collection_failed=True)
    assert clock.consecutive_collection_failures == before


def test_the_campaign_fixture_helper_builds_a_valid_campaign(guardrails):
    """Sanity check on the shared helper the other paid tests lean on."""
    campaign = make_campaign(guardrails=guardrails)
    assert campaign.campaign_id == "camp-1"
    assert campaign.guardrails is guardrails
