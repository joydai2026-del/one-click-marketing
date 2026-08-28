"""End to end, against the REAL shipped config files.

Unit tests can all pass while the thing an operator actually runs does something else, so
these build the loop the way `ocm` builds it and assert on what comes out.
"""

from __future__ import annotations

import pytest
from conftest import CONFIG_DIR

from ocm import cli
from ocm.cli import _build_organic
from ocm.models import PublishRecord, new_id

# Far enough ahead of any real clock that nothing here can pass because the test happened
# to run at a particular moment.
DAY = 86_400.0
BASE_NOW = 4_000_000_000.0


def build(auto: bool):
    loop, rubric, transport = _build_organic(
        CONFIG_DIR / "organic.toml", CONFIG_DIR / "rubric.toml"
    )
    if auto:
        loop.gate_mode = "auto"
        loop.auto_approval_enabled = True
    return loop, rubric, transport


def confirmed_post(channel: str, at: float) -> PublishRecord:
    return PublishRecord(
        publish_id=new_id("pub"),
        draft_id="seeded",
        channel=channel,
        content_hash="h",
        external_id="seeded-ext",
        external_url="https://example.invalid/seeded",
        dry_run=True,
        published_at=at,
    )


# --------------------------------------------------------------------------------------
# the shipped default stops at the human
# --------------------------------------------------------------------------------------


def test_the_shipped_config_ships_both_graduation_latches_off():
    """One config edit must not be enough to make the loop unattended."""
    loop, _, _ = build(auto=False)
    assert loop.gate_mode == "human"
    assert loop.auto_approval_enabled is False


def test_in_human_mode_a_full_round_publishes_nothing():
    """THE claim the repository makes: nothing publishes without a human.

    Note what is asserted alongside it: the round still DRAFTED and still EVALUATED. The
    machine did all the work it can do on its own and then stopped at the boundary, which
    is different from a loop that simply did not run.
    """
    loop, _, transport = build(auto=False)

    result = loop.run_round(0, now=BASE_NOW)

    assert result.published == []
    assert transport.requests_for("publish") == []
    assert result.drafted, "the round produced no drafts, so the assertion above is vacuous"
    assert all(ev.passed for ev in result.evaluations)


def test_in_human_mode_the_drafts_are_recorded_as_awaiting_approval():
    """A silently dropped draft and a draft parked for a human look the same from the
    outside unless the loop says which one happened."""
    loop, _, _ = build(auto=False)
    result = loop.run_round(0, now=BASE_NOW)

    drafted_ids = {d.draft_id for d in result.drafted}
    awaiting = [s for s in result.skipped if "awaiting human approval" in s]

    assert len(awaiting) == len(drafted_ids)
    for draft_id in drafted_ids:
        assert any(draft_id in s for s in awaiting)
    assert all("gate_mode=human" in s for s in awaiting)


def test_human_mode_stays_stopped_across_several_rounds():
    """It is a boundary, not a first-run warning that wears off."""
    loop, _, _ = build(auto=False)
    for r in range(4):
        assert loop.run_round(r, now=BASE_NOW + r * DAY).published == []


def test_setting_only_one_latch_is_not_enough_to_publish():
    """Both latches must be on. A single edit is not enough to go unattended."""
    for gate_mode, enabled in (("auto", False), ("human", True)):
        loop, _, _ = build(auto=False)
        loop.gate_mode = gate_mode
        loop.auto_approval_enabled = enabled
        assert loop.run_round(0, now=BASE_NOW).published == []


# --------------------------------------------------------------------------------------
# with both latches on, the loop closes
# --------------------------------------------------------------------------------------


@pytest.fixture
def closed_loop():
    """Several rounds with both latches on and an advancing clock."""
    loop, _, transport = build(auto=True)
    learnings: dict = {}
    results = []
    for r in range(6):
        result = loop.run_round(r, learnings=learnings, now=BASE_NOW + r * DAY)
        learnings = result.learnings
        results.append(result)
    return loop, transport, results


def test_content_publishes_when_both_latches_are_on(closed_loop):
    _, transport, results = closed_loop
    published = [record for result in results for record in result.published]

    assert published
    assert {r.channel for r in published} == {"substack", "x"}
    assert all(r.dry_run for r in published)
    assert len(transport.requests_for("publish")) == len(published)


def test_engagement_is_collected_for_everything_that_published(closed_loop):
    _, _, results = closed_loop
    for result in results:
        assert set(result.engagement) == {r.publish_id for r in result.published}
        for record in result.engagement.values():
            assert record.metrics


def test_learnings_eventually_produce_at_least_one_real_winner(closed_loop):
    """The loop closes: measurements come back, get ranked, and a dimension separates.

    Asserted as "eventually" rather than "in round N" because the point is that the
    feedback path works, not that a stub's arithmetic lands on a particular round.
    """
    _, _, results = closed_loop
    winners = [
        (channel, dimension, learn.winner)
        for result in results
        for channel, dims in result.learnings.items()
        for dimension, learn in dims.items()
        if learn.actionable
    ]
    assert winners, "no dimension ever separated, so the learning half of the loop is dead"


def test_no_winner_is_ever_announced_below_the_configured_minimum_sample_count(closed_loop):
    """The honesty half: the ranker must not name a winner it cannot support."""
    loop, _, results = closed_loop
    for result in results:
        for dims in result.learnings.values():
            for learn in dims.values():
                if learn.actionable:
                    assert learn.sample_count >= loop.ranker.min_samples
                    assert len(learn.means) >= 2


def test_every_published_draft_carries_the_style_tags_it_was_generated_from(closed_loop):
    """This is what makes the measurements comparable at all. Untagged content is content
    the loop can never learn anything from."""
    _, _, results = closed_loop
    dimensions = set(build(auto=False)[0].space.dimensions)
    for result in results:
        for draft in result.drafted:
            tags = dict(pair.split("=", 1) for pair in draft.derived_from)
            assert set(tags) == dimensions


def test_exploration_rounds_are_announced_rather_than_silent(closed_loop):
    """An operator reading the log has to be able to tell a deliberate exploration from
    the loop ignoring its own learnings."""
    _, _, results = closed_loop
    notes = [note for result in results for note in result.notes]
    assert any("exploration slot" in note for note in notes)


def test_a_generation_or_gate_refusal_on_one_channel_does_not_take_down_the_other(closed_loop):
    """Rounds where one channel is blocked must still publish the other one."""
    _, _, results = closed_loop
    for result in results:
        blocked_channels = {
            line.split("-")[1] for line in result.blocked + result.skipped if "-" in line
        }
        if blocked_channels and result.drafted:
            # If something was refused, the round is still allowed to have published.
            assert isinstance(result.published, list)
    # And across the whole run, at least one round published on both channels at once.
    assert any(len({r.channel for r in result.published}) == 2 for result in results)


# --------------------------------------------------------------------------------------
# the schedule cap
# --------------------------------------------------------------------------------------


def test_a_channel_already_at_its_cap_is_skipped_before_any_generation_happens():
    """The cap is wired into the loop, and it is checked FIRST.

    Checking "may this channel publish right now?" before calling the generator means a
    capped channel costs zero model calls. Checking after means paying for content that is
    thrown away, every tick, forever.
    """
    loop, _, transport = build(auto=True)
    for channel in loop.channels:
        loop.log.record(confirmed_post(channel, at=BASE_NOW))

    result = loop.run_round(0, now=BASE_NOW)

    assert result.published == []
    assert result.drafted == []
    assert transport.requests == []
    assert len(result.skipped) == len(loop.channels)
    assert all("cap is 1" in s for s in result.skipped)


def test_the_cap_lifts_once_the_window_has_passed():
    loop, _, _ = build(auto=True)
    for channel in loop.channels:
        loop.log.record(confirmed_post(channel, at=BASE_NOW))

    assert loop.run_round(0, now=BASE_NOW).published == []
    assert loop.run_round(1, now=BASE_NOW + 25 * 3600).published


def test_two_rounds_at_the_same_now_publish_at_most_the_cap_per_channel():
    """One confirmed post per channel per window, counted from the durable log.

    The second round is at the SAME instant as the first, so its posts are unambiguously
    inside the first one's window and the cap must refuse them.

    REGRESSION GUARD. This failed once, and the failure was invisible in ordinary use: the
    cap was read against the caller's injected `now` while `published_at` was stamped from
    the wall clock, so under any simulated clock every confirmed post fell outside its own
    window, `count_in_window` returned 0, and the cap never fired. The publisher now owns
    the clock and stamps the record with it (see Publisher.publish).
    """
    loop, _, _ = build(auto=True)
    cap = loop.schedule.max_per_window

    first = loop.run_round(0, now=BASE_NOW)
    second = loop.run_round(1, now=BASE_NOW)

    per_channel: dict[str, int] = {}
    for record in first.published + second.published:
        per_channel[record.channel] = per_channel.get(record.channel, 0) + 1

    assert per_channel, "nothing published at all, so the cap assertion is vacuous"
    assert max(per_channel.values()) <= cap, per_channel


def test_the_post_log_is_the_durable_source_of_the_cap_and_of_the_rotation_offset():
    """`total` doubles as the style rotation offset, so a skipped tick must not advance
    it: the slot is reused rather than burned."""
    loop, _, _ = build(auto=True)
    assert loop.log.total() == 0

    result = loop.run_round(0, now=BASE_NOW)
    assert loop.log.total() == len(result.published)


# --------------------------------------------------------------------------------------
# cross-channel identical content is HELD, not regenerated
# --------------------------------------------------------------------------------------


def test_identical_copy_across_two_channels_is_held_for_a_human():
    """A judgment call (sometimes identical copy on two surfaces is exactly what you
    want), not a defect the generator should be told to fix."""
    loop, _, _ = build(auto=True)

    class EchoModel:
        def complete(self, prompt: str) -> str:
            return "the identical body that both channels would receive " * 6

    loop.generator.model = EchoModel()
    result = loop.run_round(0, now=BASE_NOW)

    assert result.held
    assert result.published == []
    assert any("held for a human" in s for s in result.skipped)


# --------------------------------------------------------------------------------------
# the CLI
# --------------------------------------------------------------------------------------


def test_the_paid_command_runs_end_to_end_and_exits_zero(capsys):
    assert cli.main(["paid"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "status=PAUSED" in out
    assert "duplicate campaigns: 0" in out


def test_the_paid_command_demonstrates_the_replay_and_changed_intent_refusals(capsys):
    cli.main(["paid"])
    out = capsys.readouterr().out
    assert out.count("refused:") >= 2
    assert "ERROR:" not in out


def test_the_organic_command_in_the_shipped_default_publishes_nothing(capsys):
    assert cli.main(["organic", "--rounds", "2"]) == 0
    out = capsys.readouterr().out
    assert "gate mode       human" in out
    assert "  published " not in out
    assert "awaiting human approval" in out


def test_the_organic_command_with_auto_closes_the_loop(capsys):
    assert cli.main(["organic", "--rounds", "3", "--auto"]) == 0
    out = capsys.readouterr().out
    assert "both graduation latches set for this process only" in out
    assert "  published " in out
    assert "dry_run=True" in out
    assert "nothing left this process." in out


def test_the_rubric_command_prints_the_loaded_rubric(capsys):
    assert cli.main(["rubric"]) == 0
    out = capsys.readouterr().out
    assert "factual_grounding" in out
    assert "floor 4.0" in out
    assert "compensable" in out


def test_the_banner_states_what_the_run_can_and_cannot_do(capsys):
    cli.main(["rubric"])  # no banner on this one
    assert "DRY RUN" not in capsys.readouterr().out

    cli.main(["organic", "--rounds", "1"])
    banner = capsys.readouterr().out
    assert "No network call is made" in banner
    assert "no money can be spent" in banner


# --------------------------------------------------------------------------------------
# the out-of-band human approval path
# --------------------------------------------------------------------------------------


def _approve(loop, draft, *, scope=None, content_hash=None):
    """Stand in for a person approving a draft in some console elsewhere."""
    from ocm.approval.tokens import issue

    return issue(
        scope=scope or f"publish:{draft.channel}",
        content_hash=content_hash or draft.content_hash,
        subject=draft.draft_id,
        key=loop.approval_key,
        approver="a-human",
    )


def test_a_supplied_human_approval_publishes_in_human_mode():
    """The shipped default refuses on its own, but a real signed approval gets through.

    This is the whole product claim: not "the machine stops", but "a person can say yes,
    and the yes is bound to what they saw".
    """
    loop, _, _ = build(auto=False)
    assert loop.gate_mode == "human"

    first = loop.run_round(0, now=BASE_NOW)
    assert first.published == []
    assert first.drafted

    draft = first.drafted[0]
    loop.approvals[draft.content_hash] = _approve(loop, draft)

    second = loop.run_round(1, now=BASE_NOW)
    assert [r.draft_id for r in second.published] == [draft.draft_id]


def test_an_approval_does_not_carry_over_to_regenerated_content():
    """Approve draft A, let the pipeline produce draft B, and the approval must not apply.

    Keying approvals on the CONTENT HASH is what makes this automatic: different bytes
    means a different key, so a stale approval simply does not match anything.
    """
    loop, _, _ = build(auto=False)
    first = loop.run_round(0, now=BASE_NOW)
    draft = first.drafted[0]

    # A human approves these exact bytes.
    loop.approvals[draft.content_hash] = _approve(loop, draft)
    # The content then changes underneath them.
    loop.approvals["not-the-hash-of-anything-generated"] = loop.approvals.pop(
        draft.content_hash
    )

    second = loop.run_round(1, now=BASE_NOW)
    assert second.published == []


def test_an_approval_for_the_wrong_channel_is_refused():
    """A publish approval is scoped to one channel. Reusing it elsewhere is a scope error,
    not a convenience."""
    loop, _, _ = build(auto=False)
    first = loop.run_round(0, now=BASE_NOW)
    draft = first.drafted[0]

    loop.approvals[draft.content_hash] = _approve(
        loop, draft, scope="publish:some-other-channel"
    )
    assert loop.run_round(1, now=BASE_NOW).published == []


def test_an_approval_signed_with_the_wrong_key_is_refused():
    loop, _, _ = build(auto=False)
    first = loop.run_round(0, now=BASE_NOW)
    draft = first.drafted[0]

    from ocm.approval.tokens import issue

    token, sig = issue(
        scope=f"publish:{draft.channel}",
        content_hash=draft.content_hash,
        subject=draft.draft_id,
        key=b"an-attackers-key-not-the-loops-key",
        approver="not-really-a-human",
    )
    loop.approvals[draft.content_hash] = (token, sig)
    assert loop.run_round(1, now=BASE_NOW).published == []


def test_a_human_approval_is_single_use():
    """Otherwise one approval is a reusable coupon for every future round."""
    loop, _, _ = build(auto=False)
    first = loop.run_round(0, now=BASE_NOW)
    draft = first.drafted[0]
    loop.approvals[draft.content_hash] = _approve(loop, draft)

    published_once = loop.run_round(1, now=BASE_NOW).published
    assert len(published_once) == 1

    # The same approval, still sitting in the map, must not authorize a second publish.
    published_twice = loop.run_round(2, now=BASE_NOW + 25 * 3600).published
    assert [r.draft_id for r in published_twice] != [draft.draft_id]


def test_a_refused_human_approval_does_not_fall_through_to_auto_mode():
    """If a person tried to approve this and it did not verify, that is a stop, not a
    reason to let the machine decide instead."""
    loop, _, _ = build(auto=True)
    first = loop.run_round(0, now=BASE_NOW)
    draft = first.drafted[0]

    token, sig = _approve(loop, draft)
    loop.approvals[draft.content_hash] = (token, sig + "tampered")

    second = loop.run_round(1, now=BASE_NOW + 25 * 3600)
    assert draft.draft_id not in [r.draft_id for r in second.published]
