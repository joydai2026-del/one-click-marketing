"""The ranker and the tilt: the three ways this loop declines to invent a winner.

Computing a mean is easy. Being honest about when the mean means nothing is the module.
"""

from __future__ import annotations

import math

import pytest

from ocm.generation.style import Axis, StyleSpace
from ocm.learning.ranker import Learnings, Ranker, Sample, Tilt

CHANNEL = "substack"


def sample(hook: str, score: float | None, *, vid: str = "", channel: str = CHANNEL) -> Sample:
    return Sample(
        variant_id=vid or f"v-{hook}-{score}",
        channel=channel,
        tags={"hook": hook, "format": "list"},
        score=score,
        excerpt=f"excerpt for {hook}",
    )


def rank(samples: list[Sample], *, min_samples: int = 3, dimension: str = "hook") -> Learnings:
    return Ranker(min_samples=min_samples).rank_dimension(
        samples, channel=CHANNEL, dimension=dimension
    )


# --------------------------------------------------------------------------------------
# the three refusals
# --------------------------------------------------------------------------------------


def test_insufficient_data_below_min_samples():
    learn = rank([sample("question", 0.9), sample("story", 0.1)], min_samples=3)
    assert learn.status == "insufficient_data"
    assert learn.winner is None
    assert learn.actionable is False
    assert learn.sample_count == 2
    assert learn.means == {}


def test_low_confidence_when_every_sample_used_the_same_tag_value():
    """Nothing was compared to anything: this is not a finding, it is a coincidence."""
    learn = rank([sample("question", s) for s in (0.9, 0.8, 0.7)])
    assert learn.status == "low_confidence"
    assert learn.winner is None
    assert set(learn.means) == {"question"}


def test_low_confidence_on_an_exact_tie_at_the_top():
    """Naming either side would be a coin flip presented as a finding, and the loop would
    then chase it for every future round."""
    learn = rank(
        [
            sample("question", 0.8, vid="a"),
            sample("story", 0.8, vid="b"),
            sample("number", 0.1, vid="c"),
        ]
    )
    assert learn.status == "low_confidence"
    assert learn.winner is None


def test_a_tie_within_floating_point_noise_is_still_a_tie():
    """Means equal in intent can differ in the last bits; MIN_SEPARATION absorbs that."""
    learn = rank(
        [
            sample("question", 0.1 + 0.2, vid="a"),
            sample("story", 0.3, vid="b"),
            sample("number", 0.0, vid="c"),
        ]
    )
    assert learn.winner is None
    assert learn.status == "low_confidence"


def test_a_real_winner_when_one_value_strictly_beats_the_runner_up():
    learn = rank(
        [
            sample("question", 0.9, vid="a"),
            sample("question", 0.8, vid="b"),
            sample("story", 0.2, vid="c"),
            sample("number", 0.1, vid="d"),
        ]
    )
    assert learn.status == "winner"
    assert learn.winner == "question"
    assert learn.actionable is True
    assert learn.means["question"] == pytest.approx(0.85)
    assert learn.sample_count == 4


def test_the_winner_is_decided_by_the_mean_not_by_the_count_of_samples():
    """Otherwise the loop learns "whatever we posted most", which it chose itself."""
    learn = rank(
        [
            sample("story", 0.1, vid=f"s{i}") for i in range(5)
        ]
        + [sample("question", 0.9, vid="q1")]
    )
    assert learn.winner == "question"


# --------------------------------------------------------------------------------------
# absent is not zero
# --------------------------------------------------------------------------------------


def test_a_sample_with_no_score_is_dropped_from_the_count():
    learn = rank([sample("question", 0.9), sample("story", None), sample("number", 0.5)])
    assert learn.sample_count == 2


def test_absent_is_not_zero_and_a_missing_measurement_cannot_flip_the_winner():
    """THE invariant. Treating "not measured" as "measured badly" would teach the loop to
    avoid whatever the collector is currently failing to read, which is the exact opposite
    of the truth.

    Construction: `story` genuinely outperforms `question`, but has two unmeasured items.
    Scored as zeros, story's mean drops to 0.30 and question wins. Dropped, story keeps its
    0.9 mean and wins correctly.
    """
    samples = [
        sample("story", 0.9, vid="s1"),
        sample("story", 0.9, vid="s2"),
        sample("story", None, vid="s3"),
        sample("story", None, vid="s4"),
        sample("question", 0.5, vid="q1"),
        sample("question", 0.5, vid="q2"),
    ]

    learn = rank(samples)

    assert learn.winner == "story"
    assert learn.means["story"] == pytest.approx(0.9)
    assert learn.sample_count == 4

    # And the counterfactual: coercing None to 0.0 really would have flipped it.
    coerced = [
        Sample(s.variant_id, s.channel, s.tags, 0.0 if s.score is None else s.score, s.excerpt)
        for s in samples
    ]
    assert rank(coerced).winner == "question"


def test_a_genuine_zero_is_kept_and_stays_distinguishable_from_a_missing_reading():
    """A post that really got nothing is evidence. A post nobody measured is not."""
    measured_zero = rank(
        [
            sample("question", 0.6, vid="a"),
            sample("story", 0.0, vid="b"),
            sample("story", 0.0, vid="c"),
        ]
    )
    assert measured_zero.sample_count == 3
    assert measured_zero.means["story"] == 0.0
    assert measured_zero.winner == "question"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_scores_are_dropped_rather_than_ranked(bad):
    """A NaN would silently poison every comparison it takes part in."""
    usable = Ranker().usable(
        [sample("question", 0.5, vid="a"), sample("story", bad, vid="b")], CHANNEL
    )
    assert [s.variant_id for s in usable] == ["a"]
    assert all(math.isfinite(s.score) for s in usable)


def test_samples_from_another_channel_are_never_mixed_in():
    """Cross-channel counters are not comparable, which is why normalize lives on the
    adapter in the first place."""
    learn = rank(
        [
            sample("question", 0.9, vid="a"),
            sample("story", 0.1, vid="b"),
            sample("number", 0.1, vid="c"),
            sample("story", 1.0, vid="d", channel="x"),
        ]
    )
    assert learn.sample_count == 3
    assert learn.winner == "question"


def test_samples_missing_the_dimension_tag_are_excluded():
    untagged = Sample("u1", CHANNEL, {"format": "list"}, 1.0, "e")
    learn = rank([sample("question", 0.9), sample("story", 0.1), untagged])
    assert learn.sample_count == 2


# --------------------------------------------------------------------------------------
# the signal handed to the next round
# --------------------------------------------------------------------------------------


def test_the_guidance_string_says_scored_higher_and_never_claims_causal_lift():
    """A dimension winner is a correlation over a small non-randomized sample."""
    samples = [
        sample("question", 0.9, vid="a"),
        sample("question", 0.8, vid="b"),
        sample("story", 0.1, vid="c"),
    ]
    ranker = Ranker(min_samples=3)
    learnings = ranker.rank_all(samples, channel=CHANNEL, dimensions=("hook",))
    signal = ranker.signal(
        channel=CHANNEL, round_index=2, samples=samples, learnings=learnings
    )

    text = " ".join(signal.guidance)
    assert "scored highest" in text
    assert "not a measured causal lift" in text
    assert "because" not in text.replace("not a measured causal lift", "")


def test_a_dimension_with_no_winner_is_told_to_keep_rotating():
    ranker = Ranker(min_samples=3)
    samples = [sample("question", s, vid=f"v{i}") for i, s in enumerate((0.9, 0.8, 0.7))]
    learnings = ranker.rank_all(samples, channel=CHANNEL, dimensions=("hook",))
    signal = ranker.signal(channel=CHANNEL, round_index=0, samples=samples, learnings=learnings)
    assert "no winner (low_confidence" in signal.guidance[0]
    assert "keep rotating" in signal.guidance[0]


def test_examples_are_bounded_so_the_brief_cannot_become_the_corpus():
    """A signal is an instruction to the generator, not a dump of history, so a bad round
    cannot poison an unbounded amount of future work."""
    ranker = Ranker(min_samples=1, top_k=2, bottom_k=1, excerpt_chars=40)
    samples = [
        Sample(f"v{i}", CHANNEL, {"hook": "question"}, i / 10, "word " * 200) for i in range(6)
    ]
    signal = ranker.signal(channel=CHANNEL, round_index=0, samples=samples, learnings={})
    assert len(signal.top_examples) == 2
    assert len(signal.weak_examples) == 1
    for excerpt in signal.top_examples + signal.weak_examples:
        assert len(excerpt) <= 40 + len("...")


def test_from_config_reads_the_learning_parameters():
    r = Ranker.from_config(
        {"min_samples": 7, "top_k": 4, "bottom_k": 2, "excerpt_chars": 100}
    )
    assert (r.min_samples, r.top_k, r.bottom_k, r.excerpt_chars) == (7, 4, 2, 100)


# --------------------------------------------------------------------------------------
# the tilt
# --------------------------------------------------------------------------------------


@pytest.fixture
def space() -> StyleSpace:
    return StyleSpace(
        axes=(
            Axis("hook", ("question", "number", "story")),
            Axis("format", ("short", "list")),
        )
    )


def winner(dimension: str, value: str) -> Learnings:
    return Learnings(
        channel=CHANNEL, dimension=dimension, winner=value, means={value: 1.0}, sample_count=9
    )


def normal_position(space: StyleSpace) -> int:
    cycle, positions = space.exploration_positions()
    for pos in range(cycle * 4):
        if pos % cycle not in positions:
            return pos
    raise AssertionError("this space reserves every position for exploration")


def test_tilt_applies_a_winner_on_a_normal_position(space):
    pos = normal_position(space)
    tilt = Tilt()
    style, exploring = tilt.style_for_round(space, pos, {"hook": winner("hook", "story")})

    assert exploring is False
    assert style.value("hook") == "story"
    # Untouched dimensions keep the plain rotation, so the space is still walked.
    assert style.value("format") == space.rotation_at(pos, 1)[0].value("format")
    assert tilt.applied_winners == {"hook": "story"}


def test_tilt_ignores_every_winner_on_an_exploration_position(space):
    """A loop that always plays the current winner stops learning immediately: its first
    accidental winner is its winner forever."""
    _, positions = space.exploration_positions()
    pos = min(positions)
    tilt = Tilt()

    style, exploring = tilt.style_for_round(space, pos, {"hook": winner("hook", "story")})

    assert exploring is True
    assert style == space.rotation_at(pos, 1)[0]
    assert tilt.applied_winners == {}


def test_tilt_refuses_a_winner_that_is_not_a_value_of_that_axis(space):
    """Stale-learning guard: a persisted learning from an older config must never inject
    an unknown coordinate into a live plan."""
    pos = normal_position(space)
    tilt = Tilt()
    style, exploring = tilt.style_for_round(
        space, pos, {"hook": winner("hook", "retired-hook-value")}
    )

    assert exploring is False
    assert style == space.rotation_at(pos, 1)[0]
    assert tilt.applied_winners == {}


def test_tilt_refuses_a_winner_for_an_axis_that_no_longer_exists(space):
    pos = normal_position(space)
    tilt = Tilt()
    style, _ = tilt.style_for_round(space, pos, {"removed_axis": winner("removed_axis", "v")})
    assert style == space.rotation_at(pos, 1)[0]
    assert tilt.applied_winners == {}


def test_tilt_ignores_a_non_actionable_learning(space):
    pos = normal_position(space)
    tilt = Tilt()
    stalled = Learnings(
        channel=CHANNEL, dimension="hook", winner=None, means={}, sample_count=1,
        insufficient_data=True,
    )
    style, _ = tilt.style_for_round(space, pos, {"hook": stalled})
    assert style == space.rotation_at(pos, 1)[0]
    assert tilt.applied_winners == {}


def test_applied_winners_records_only_what_was_actually_applied(space):
    """An honest provenance record: a refused winner must not appear as if it shaped the
    draft."""
    pos = normal_position(space)
    tilt = Tilt()
    tilt.style_for_round(
        space,
        pos,
        {"hook": winner("hook", "story"), "format": winner("format", "not-a-format")},
    )
    assert tilt.applied_winners == {"hook": "story"}


def test_applied_winners_is_cleared_when_the_next_position_explores(space):
    """Otherwise a report attributes an exploration draft to last round's winners."""
    tilt = Tilt()
    tilt.style_for_round(space, normal_position(space), {"hook": winner("hook", "story")})
    assert tilt.applied_winners == {"hook": "story"}

    exploring_position = min(space.exploration_positions()[1])
    tilt.style_for_round(space, exploring_position, {"hook": winner("hook", "story")})
    assert tilt.applied_winners == {}


def test_applied_winners_returns_a_copy_that_cannot_mutate_the_tilt(space):
    tilt = Tilt()
    tilt.style_for_round(space, normal_position(space), {"hook": winner("hook", "story")})
    tilt.applied_winners["hook"] = "tampered"
    assert tilt.applied_winners == {"hook": "story"}
