"""The scoring rubric.

The idea being demonstrated: a generator that judges its own output has no error signal,
so quality is scored against a rubric that is WRITTEN DOWN, VERSIONED, and LOADED FROM
CONFIG rather than living inside a prompt. Ten dimensions, each scored 0 to 5, each with a
weight, and some with an individual floor. That gives three separate levers an operator can
turn without editing code: what is measured, how much it counts, and how bad is too bad.

The ten dimensions shipped in `config/example/rubric.toml` are generic marketing-content
dimensions. A real deployment replaces them with its own; the file is the interface.

Scoring is pluggable. `Rubric.score` takes a `Scorer` callable so that the same rubric can
be driven by a language model in production and by a deterministic heuristic in the
dry-run, with identical downstream behavior. That substitution is the whole reason this
repository can run end to end with no API key.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..models import DimensionScore

MAX_DIMENSION_SCORE = 5.0


@dataclass(frozen=True)
class Dimension:
    """One thing the rubric measures.

    Args:
        name: stable identifier, used in reports and in learning signals.
        description: what a 5 looks like. This is the text handed to an LLM scorer.
        weight: relative contribution to the weighted total. Weights are normalized, so
            they do not have to sum to 1 in config.
        floor: optional per-dimension minimum. A dimension below its floor is a hard
            failure even if the weighted total is high. Use it for the dimensions where
            a bad score is not compensable, for example factual accuracy.
    """

    name: str
    description: str
    weight: float = 1.0
    floor: float | None = None


class Scorer(Protocol):
    """Assigns a 0-5 score and a short note for one dimension of one text."""

    def __call__(self, dimension: Dimension, text: str) -> tuple[float, str]: ...


@dataclass
class Rubric:
    """A versioned set of weighted dimensions plus an overall pass threshold.

    `threshold` is expressed on the same 0-5 scale as the dimensions, so that "we require
    an average of 3.5" is readable rather than being an opaque normalized fraction.
    """

    version: str
    dimensions: list[Dimension] = field(default_factory=list)
    threshold: float = 3.5

    @classmethod
    def from_config(cls, cfg: dict) -> Rubric:
        dims = [
            Dimension(
                name=d["name"],
                description=d.get("description", ""),
                weight=float(d.get("weight", 1.0)),
                floor=(float(d["floor"]) if d.get("floor") is not None else None),
            )
            for d in cfg.get("dimensions", [])
        ]
        if not dims:
            raise ValueError("rubric config defines no dimensions")
        if any(d.weight < 0 for d in dims):
            raise ValueError("rubric weights must be non-negative")
        if sum(d.weight for d in dims) <= 0:
            raise ValueError("rubric weights must not all be zero")
        return cls(
            version=str(cfg.get("version", "unversioned")),
            dimensions=dims,
            threshold=float(cfg.get("threshold", 3.5)),
        )

    def score(self, text: str, scorer: Scorer) -> tuple[float, list[DimensionScore], list[str]]:
        """Score `text`, returning (weighted_score, per_dimension, floor_failures)."""
        results: list[DimensionScore] = []
        floor_failures: list[str] = []
        total_weight = sum(d.weight for d in self.dimensions)

        weighted = 0.0
        for dim in self.dimensions:
            raw, note = scorer(dim, text)
            raw = max(0.0, min(MAX_DIMENSION_SCORE, float(raw)))
            results.append(DimensionScore(name=dim.name, score=raw, weight=dim.weight, note=note))
            weighted += raw * dim.weight
            if dim.floor is not None and raw < dim.floor:
                floor_failures.append(
                    f"dimension {dim.name!r} scored {raw:.1f}, floor is {dim.floor:.1f}"
                )

        return weighted / total_weight, results, floor_failures


STUB_SCORE = 4.5


def stub_scorer(
    overrides: dict[str, float] | None = None,
) -> Callable[[Dimension, str], tuple[float, str]]:
    """The dry-run stand-in for a judge. NOT a quality model.

    Read this carefully, because it is the one place the demo could mislead.

    In production the scorer is a language model reading the rubric dimension's
    description and grading the text. That call is the only part of the evaluation stage
    this repository does not implement, because implementing it would require a key, a
    vendor, and a network, and none of those belong in a portfolio dry-run.

    This stub returns a FIXED passing value for every dimension. It is deliberately NOT a
    pseudo-random score, because a pseudo-random score looks like a judgment and is not
    one: it would trip a real compliance floor at random, printing "factual grounding
    below floor" about text nothing ever assessed. A constant is honest. It says: no
    quality judgment was made here.

    Everything AROUND the score is real and is exercised by the demo and the tests: the
    weighting arithmetic, the per-dimension floors, the threshold comparison, and the
    short-circuit ordering that skips scoring entirely once a hard floor has tripped.

    `overrides` lets a test or a demo force any dimension to any value, which is how the
    failing paths are exercised deterministically.
    """
    forced = overrides or {}

    def _score(dimension: Dimension, text: str) -> tuple[float, str]:
        if dimension.name in forced:
            return forced[dimension.name], "score forced by caller"
        return STUB_SCORE, "dry-run stub: no quality judgment was made"

    return _score
