"""Deterministic work-item identity.

Identity comes from the SLOT INPUTS, never from the generated output. Generation is
non-deterministic, so an identity derived from the text gives a crash-and-retry a brand new
id, the store does not recognize it, and the pipeline publishes a second copy of something
a human already approved.
"""

from __future__ import annotations

import pytest

from ocm.generation.identity import SLOT_ID_LEN, assert_unique_slots, slot_id, variant_id


def test_slot_id_is_stable_for_the_same_inputs():
    """A retry of the same tick must compute the same id, or it stages duplicate work."""
    assert slot_id("question.list", "topic-a") == slot_id("question.list", "topic-a")


def test_slot_id_does_not_depend_on_generated_text():
    """Stated as a property of the signature: text is not an input at all."""
    with pytest.raises(TypeError):
        slot_id("question.list", "topic-a", "some generated body")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "a,b",
    [
        (("question.list", "topic-a"), ("question.list", "topic-b")),
        (("question.list", "topic-a"), ("story.list", "topic-a")),
        (("question.list", "topic-a"), ("question.short", "topic-a")),
    ],
)
def test_slot_id_differs_when_any_input_differs(a, b):
    assert slot_id(*a) != slot_id(*b)


def test_nul_separation_stops_a_field_boundary_collision():
    """Concatenating without a separator makes ("a.b", "c") and ("a", "b.c") identical.

    The two are genuinely different pieces of planned work: a colliding id means one of
    them silently overwrites the other and a planned post disappears with no error.
    """
    assert slot_id("a.b", "c") != slot_id("a", "b.c")


def test_nul_separation_holds_for_the_empty_field_case():
    assert slot_id("", "ab") != slot_id("ab", "")
    assert slot_id("a", "") != slot_id("", "a")


def test_slot_id_is_hex_of_the_declared_length():
    sid = slot_id("question.list", "topic-a")
    assert len(sid) == SLOT_ID_LEN
    assert set(sid) <= set("0123456789abcdef")


def test_variant_id_format_is_run_channel_slot():
    assert variant_id("r3", "substack", "abc123def456") == "r3-substack-abc123def456"


def test_variant_id_distinguishes_the_same_slot_across_runs_and_channels():
    """The slot is the same piece of planned content; the run and channel make it a
    distinct work item, so all three parts have to appear."""
    slot = slot_id("question.list", "topic-a")
    ids = {
        variant_id("r0", "substack", slot),
        variant_id("r1", "substack", slot),
        variant_id("r0", "x", slot),
    }
    assert len(ids) == 3


# --------------------------------------------------------------------------------------
# collision detection
# --------------------------------------------------------------------------------------


def test_assert_unique_slots_passes_for_genuinely_distinct_slots():
    assert_unique_slots(
        [("question.list", "topic-a"), ("question.list", "topic-b"), ("story.list", "topic-a")]
    )


def test_assert_unique_slots_accepts_an_empty_plan():
    assert_unique_slots([])


def test_assert_unique_slots_raises_loudly_on_a_repeated_pair():
    """Loud, because the alternative is one planned item silently disappearing."""
    with pytest.raises(ValueError, match="slot collision"):
        assert_unique_slots([("question.list", "topic-a"), ("question.list", "topic-a")])


def test_the_collision_message_names_both_colliding_slots():
    """An operator has to be able to see WHICH two pieces of work collided."""
    with pytest.raises(ValueError) as exc:
        assert_unique_slots(
            [("s", "topic-a"), ("other", "topic-b"), ("s", "topic-a")]
        )
    message = str(exc.value)
    assert "topic-a" in message and message.count("topic-a") >= 2
    assert "topic-b" not in message
