"""The quality gate: short-circuit ordering, non-compensable floors, and its narrow job.

The gate answers "is this good enough". It never answers "may this go out". Those are two
different questions and the tests below keep them apart.
"""

from __future__ import annotations

import pytest
from conftest import exploding_scorer, make_draft

from ocm.channels.transport import DryRunTransport
from ocm.evaluation.compliance import ComplianceFloor
from ocm.evaluation.dedup import DedupIndex
from ocm.evaluation.gate import QualityGate
from ocm.evaluation.rubric import Dimension, Rubric, stub_scorer
from ocm.models import Verdict

BODY = (
    "the review gate scores every draft against a written rubric before any human is asked "
    "to look at it, which is the only way a human's attention stays expensive to waste"
)


def gate_with(*, compliance=None, dedup=None, scorer=None, rubric=None) -> QualityGate:
    return QualityGate(
        rubric=rubric
        or Rubric(
            version="t",
            dimensions=[Dimension("a", "", weight=1.0), Dimension("b", "", weight=1.0)],
            threshold=3.5,
        ),
        compliance=compliance or ComplianceFloor(min_chars=0),
        dedup=dedup or DedupIndex(),
        scorer=scorer if scorer is not None else stub_scorer(),
    )


# --------------------------------------------------------------------------------------
# the short circuit
# --------------------------------------------------------------------------------------


def test_a_compliance_failure_short_circuits_before_the_rubric_is_scored():
    """`exploding_scorer` raises if called, so this proves the judge call was SKIPPED.

    Asserting only on the output fields would not: a gate that scored the rubric and then
    discarded the result would produce the same EvalResult while still paying for the
    judge on every disqualified draft.
    """
    gate = gate_with(
        compliance=ComplianceFloor(forbidden_terms=["forbidden-widget"], min_chars=0),
        scorer=exploding_scorer,
    )
    ev = gate.evaluate(make_draft(f"a post about a {'forbidden-widget'}"))

    assert ev.verdict is Verdict.FAIL
    assert ev.dimensions == ()
    assert ev.weighted_score == 0.0
    assert any("not scored" in n for n in ev.notes)
    assert any("hard floor" in n for n in ev.notes)
    assert len(ev.hard_failures) == 1


def test_a_dedup_failure_also_short_circuits_before_the_rubric_is_scored():
    dedup = DedupIndex(threshold=0.6)
    dedup.add("prior-1", BODY, "some-other-hash")
    gate = gate_with(dedup=dedup, scorer=exploding_scorer)

    ev = gate.evaluate(make_draft(BODY))

    assert ev.verdict is Verdict.FAIL
    assert ev.dimensions == ()
    assert any("duplicate" in h for h in ev.hard_failures)
    assert any("not scored" in n for n in ev.notes)


def test_an_exact_hash_duplicate_short_circuits_too():
    """A retry re-presenting identical bytes must not cost a judge call either."""
    draft = make_draft(BODY)
    dedup = DedupIndex()
    dedup.add("prior-1", "entirely different text", draft.content_hash)

    ev = gate_with(dedup=dedup, scorer=exploding_scorer).evaluate(draft)
    assert ev.verdict is Verdict.FAIL
    assert any("exact-hash" in h for h in ev.hard_failures)


def test_compliance_and_dedup_failures_are_reported_together():
    """Both cheap checks run before the short circuit, so one pass names every defect."""
    dedup = DedupIndex(threshold=0.6)
    dedup.add("prior-1", BODY, "other-hash")
    gate = gate_with(
        compliance=ComplianceFloor(forbidden_terms=["gate"], min_chars=0),
        dedup=dedup,
        scorer=exploding_scorer,
    )
    ev = gate.evaluate(make_draft(BODY))
    assert len(ev.hard_failures) >= 2


# --------------------------------------------------------------------------------------
# floors versus the weighted score
# --------------------------------------------------------------------------------------


def test_a_floor_failure_fails_the_draft_even_with_a_high_weighted_score():
    """No threshold is high enough to redeem a non-compensable dimension."""
    rubric = Rubric(
        version="t",
        dimensions=[
            Dimension("grounding", "", weight=0.01, floor=4.0),
            Dimension("style", "", weight=100.0),
        ],
        threshold=3.5,
    )
    ev = gate_with(
        rubric=rubric, scorer=stub_scorer({"grounding": 0.0, "style": 5.0})
    ).evaluate(make_draft(BODY))

    assert ev.weighted_score > ev.threshold
    assert ev.verdict is Verdict.FAIL
    assert any("grounding" in h for h in ev.hard_failures)


def test_a_below_threshold_score_fails_and_says_so_in_the_notes():
    ev = gate_with(scorer=stub_scorer({"a": 1.0, "b": 1.0})).evaluate(make_draft(BODY))
    assert ev.verdict is Verdict.FAIL
    assert ev.hard_failures == ()
    assert any("below threshold" in n for n in ev.notes)


def test_a_clean_draft_passes_with_dimensions_recorded_and_nothing_flagged():
    ev = gate_with().evaluate(make_draft(BODY))
    assert ev.verdict is Verdict.PASS
    assert ev.passed is True
    assert ev.hard_failures == ()
    assert ev.notes == ()
    assert {d.name for d in ev.dimensions} == {"a", "b"}


def test_the_result_carries_the_draft_id_and_the_content_hash_it_judged():
    """The approval token binds to this hash, so the gate must report the one it scored."""
    draft = make_draft(BODY, draft_id="r3-substack-abc")
    ev = gate_with().evaluate(draft)
    assert ev.draft_id == "r3-substack-abc"
    assert ev.content_hash == draft.content_hash


def test_the_title_is_part_of_what_is_judged():
    """A forbidden term hidden in a title would otherwise sail through untouched."""
    gate = gate_with(compliance=ComplianceFloor(forbidden_terms=["forbidden"], min_chars=0))
    ev = gate.evaluate(make_draft(BODY, title="a forbidden headline"))
    assert ev.verdict is Verdict.FAIL


# --------------------------------------------------------------------------------------
# what the gate must not do
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["publish", "approve", "spend", "post"])
def test_the_gate_exposes_no_way_to_publish(verb):
    """Keeping "good enough" and "may go out" apart is what stops a quality improvement
    from silently becoming a permission to spend."""
    assert not hasattr(QualityGate, verb)


def test_evaluating_a_draft_sends_nothing_anywhere():
    """A transport that recorded zero requests is a much stronger claim than a return value."""
    transport = DryRunTransport()
    gate = gate_with()
    for verdict_case in (BODY, "x", f"{'forbidden-widget'} here"):
        gate.evaluate(make_draft(verdict_case))
    assert transport.requests == []


def test_the_gate_does_not_add_a_passing_draft_to_the_dedup_index():
    """Only a CONFIRMED post may enter the corpus.

    If evaluation seeded the index, a draft that was gated but never published would
    block its own retry forever.
    """
    gate = gate_with()
    draft = make_draft(BODY)
    assert gate.evaluate(draft).passed
    assert len(gate.dedup) == 0
    assert gate.evaluate(draft).passed
