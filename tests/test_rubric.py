"""The rubric: weight normalization, non-compensable floors, and config validation."""

from __future__ import annotations

import pytest
from conftest import make_draft

from ocm.evaluation.compliance import ComplianceFloor
from ocm.evaluation.dedup import DedupIndex
from ocm.evaluation.gate import QualityGate
from ocm.evaluation.rubric import MAX_DIMENSION_SCORE, STUB_SCORE, Dimension, Rubric, stub_scorer
from ocm.models import Verdict


def rubric_of(*dims: Dimension, threshold: float = 3.5) -> Rubric:
    return Rubric(version="t", dimensions=list(dims), threshold=threshold)


# --------------------------------------------------------------------------------------
# weighting
# --------------------------------------------------------------------------------------


def test_weights_are_normalized_so_config_need_not_sum_to_one():
    """Config authors set relative importance, not a probability distribution.

    Weights of 3 and 1 must give the same answer as 0.75 and 0.25, or an operator raising
    one dimension's weight would silently rescale the whole threshold.
    """
    dims = (Dimension("a", "", weight=3.0), Dimension("b", "", weight=1.0))
    scaled = (Dimension("a", "", weight=0.75), Dimension("b", "", weight=0.25))
    scorer = stub_scorer({"a": 4.0, "b": 0.0})

    raw, _, _ = rubric_of(*dims).score("text", scorer)
    norm, _, _ = rubric_of(*scaled).score("text", scorer)

    assert raw == pytest.approx(3.0)  # (4*3 + 0*1) / 4
    assert raw == pytest.approx(norm)


def test_weighted_score_is_the_weighted_mean_on_the_zero_to_five_scale():
    weighted, dims, floors = rubric_of(
        Dimension("a", "", weight=2.0), Dimension("b", "", weight=1.0)
    ).score("text", stub_scorer({"a": 5.0, "b": 2.0}))
    assert weighted == pytest.approx(4.0)
    assert [(d.name, d.score, d.weight) for d in dims] == [("a", 5.0, 2.0), ("b", 2.0, 1.0)]
    assert floors == []


def test_a_zero_weight_dimension_is_still_reported_but_does_not_move_the_score():
    """Reported, because an operator who zeroed a weight still wants to see the reading."""
    weighted, dims, _ = rubric_of(
        Dimension("a", "", weight=1.0), Dimension("ignored", "", weight=0.0)
    ).score("text", stub_scorer({"a": 4.0, "ignored": 0.0}))
    assert weighted == pytest.approx(4.0)
    assert {d.name for d in dims} == {"a", "ignored"}


@pytest.mark.parametrize("raw,expected", [(-5.0, 0.0), (0.0, 0.0), (99.0, MAX_DIMENSION_SCORE)])
def test_a_scorer_returning_an_out_of_range_value_is_clamped(raw, expected):
    """A misbehaving judge must not be able to push the weighted mean outside 0-5."""
    _, dims, _ = rubric_of(Dimension("a", "", weight=1.0)).score(
        "text", stub_scorer({"a": raw})
    )
    assert dims[0].score == expected


# --------------------------------------------------------------------------------------
# floors
# --------------------------------------------------------------------------------------


def test_a_floor_failure_is_returned_even_when_the_weighted_score_is_high():
    """The whole point of a floor: it is NOT compensable by other dimensions.

    Here the floored dimension carries almost no weight, so the weighted mean stays near
    the top of the scale while the floor is on the ground.
    """
    weighted, _, floors = rubric_of(
        Dimension("grounding", "", weight=0.01, floor=4.0),
        Dimension("style", "", weight=100.0),
    ).score("text", stub_scorer({"grounding": 0.0, "style": 5.0}))

    assert weighted > 4.9
    assert len(floors) == 1
    assert "grounding" in floors[0]


def test_a_dimension_exactly_at_its_floor_passes():
    """Floors are minimums, not exclusive bounds; an off-by-one here rejects good work."""
    _, _, floors = rubric_of(Dimension("a", "", weight=1.0, floor=4.0)).score(
        "text", stub_scorer({"a": 4.0})
    )
    assert floors == []


def test_every_floor_breach_is_reported_not_only_the_first():
    _, _, floors = rubric_of(
        Dimension("a", "", weight=1.0, floor=4.0),
        Dimension("b", "", weight=1.0, floor=4.0),
    ).score("text", stub_scorer({"a": 0.0, "b": 1.0}))
    assert len(floors) == 2


def test_a_dimension_without_a_floor_never_produces_a_floor_failure():
    _, _, floors = rubric_of(Dimension("a", "", weight=1.0)).score(
        "text", stub_scorer({"a": 0.0})
    )
    assert floors == []


# --------------------------------------------------------------------------------------
# the threshold
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "score,expected",
    [(3.49, Verdict.FAIL), (3.5, Verdict.PASS), (3.51, Verdict.PASS)],
)
def test_threshold_is_the_pass_boundary_and_is_inclusive(score, expected):
    """Comparison lives in the gate, so it is exercised where it actually runs.

    `weighted < threshold` means a draft landing exactly on the threshold passes. Pinned
    because flipping it to `<=` silently raises the bar for every deployment.
    """
    gate = QualityGate(
        rubric=rubric_of(Dimension("a", "", weight=1.0), threshold=3.5),
        compliance=ComplianceFloor(min_chars=0),
        dedup=DedupIndex(),
        scorer=stub_scorer({"a": score}),
    )
    assert gate.evaluate(make_draft("body")).verdict is expected


# --------------------------------------------------------------------------------------
# config validation
# --------------------------------------------------------------------------------------


def test_from_config_reads_dimensions_weights_floors_and_threshold():
    r = Rubric.from_config(
        {
            "version": "2026.01",
            "threshold": 4.0,
            "dimensions": [
                {"name": "a", "description": "d-a", "weight": 2.0, "floor": 3.0},
                {"name": "b", "description": "d-b"},
            ],
        }
    )
    assert r.version == "2026.01" and r.threshold == 4.0
    assert r.dimensions[0] == Dimension("a", "d-a", 2.0, 3.0)
    # An omitted weight defaults to 1.0 and an omitted floor means compensable, not zero.
    assert r.dimensions[1] == Dimension("b", "d-b", 1.0, None)


@pytest.mark.parametrize(
    "cfg,why",
    [
        ({"dimensions": []}, "no dimensions"),
        ({}, "dimensions key absent entirely"),
        ({"dimensions": [{"name": "a", "weight": -1.0}]}, "negative weight"),
        (
            {"dimensions": [{"name": "a", "weight": 0.0}, {"name": "b", "weight": 0.0}]},
            "all-zero weights would divide by zero",
        ),
    ],
)
def test_from_config_refuses_a_rubric_that_cannot_score_anything(cfg, why):
    """Each of these would otherwise produce a rubric that silently measures nothing."""
    with pytest.raises(ValueError):
        Rubric.from_config(cfg)


def test_the_shipped_rubric_config_loads_and_has_floors_on_the_non_compensable_dimensions():
    """Guards the claim in the config's own comments against future edits."""
    from conftest import CONFIG_DIR

    from ocm import config as cfgmod

    r = Rubric.from_config(cfgmod.load_raw(CONFIG_DIR / "rubric.toml"))
    floored = {d.name for d in r.dimensions if d.floor is not None}
    assert {"factual_grounding", "disclosure_and_compliance"} <= floored
    assert sum(d.weight for d in r.dimensions) > 0


# --------------------------------------------------------------------------------------
# the stub scorer
# --------------------------------------------------------------------------------------


def test_stub_scorer_returns_the_constant_for_every_dimension():
    """A constant is honest: it says no quality judgment was made.

    A pseudo-random score would look like a judgment, and would trip real floors at
    random about text nothing ever assessed.
    """
    scorer = stub_scorer()
    a = scorer(Dimension("a", ""), "any text")
    b = scorer(Dimension("b", ""), "completely different text")
    assert a[0] == b[0] == STUB_SCORE
    assert "no quality judgment" in a[1]


def test_stub_scorer_honors_overrides_and_leaves_other_dimensions_at_the_constant():
    scorer = stub_scorer({"a": 1.0})
    assert scorer(Dimension("a", ""), "t")[0] == 1.0
    assert scorer(Dimension("b", ""), "t")[0] == STUB_SCORE


def test_stub_scorer_note_marks_a_forced_score_as_forced():
    """So a report can never present a test override as if it were a judgment."""
    assert stub_scorer({"a": 1.0})(Dimension("a", ""), "t")[1] == "score forced by caller"
