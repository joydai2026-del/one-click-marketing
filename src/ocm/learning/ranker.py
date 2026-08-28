"""Rank style dimensions from measured engagement, and refuse to invent a winner.

This is the module that closes the loop. Everything upstream exists to make its input
comparable; everything downstream consumes its output.

WHAT IT DOES

Every published item carries the style tags it was generated from. Every measurement
carries a normalized 0-1 score from its channel adapter. For one dimension (say "hook"),
group the samples by tag value, take the mean of each group, and see whether one value
actually beat the others.

WHAT IT REFUSES TO DO

The hard part is not computing a mean. It is being honest about when the mean means
nothing. Three separate ways this ranker declines to produce a winner:

    insufficient_data   fewer than `min_samples` usable measurements
    low_confidence      enough samples, but only ONE distinct tag value was ever tried,
                        so nothing was compared against anything
    no_separation       two or more values WERE compared and came out level: the top mean
                        does not strictly beat the runner-up

They are distinct states, not one "failed" flag, because they call for different responses:
insufficient data means keep gathering, low confidence means the rotation is not spreading
across this axis, and no separation is itself a real finding, namely that these options are
equivalent so far.

Every one of them yields `winner=None`, and every consumer of a `Learnings` refuses to
tilt on a None winner. That is what stops the loop from converging on noise, which is the
characteristic failure of an unsupervised optimization loop left running: it finds a random
early winner, plays it forever, and its operator reads the resulting flat line as a plateau
rather than as a bug.

ABSENT IS NOT ZERO

A published item with no measurement yet is DROPPED from the sample, not scored 0. Treating
"not measured" as "measured badly" would teach the loop to avoid whatever the collector is
currently failing to read, which is the exact opposite of the truth.

WHAT THIS DOES NOT CLAIM

A dimension winner is a correlation over a small non-randomized sample. It is not causal
lift. Real lift needs a holdout, which this loop does not run, and the guidance strings say
"scored higher", never "performed better because".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..models import LearningSignal

# Floating point means that are equal in intent can differ in the last bits. A winner must
# beat the runner-up by more than this to count as a real separation.
MIN_SEPARATION = 1e-9


@dataclass(frozen=True)
class Sample:
    """One measured published item: its style tags and its normalized score."""

    variant_id: str
    channel: str
    tags: dict[str, str]
    score: float | None
    excerpt: str = ""


@dataclass(frozen=True)
class Learnings:
    """The verdict for ONE dimension on ONE channel."""

    channel: str
    dimension: str
    winner: str | None
    means: dict[str, float]
    sample_count: int
    insufficient_data: bool = False
    low_confidence: bool = False

    @property
    def actionable(self) -> bool:
        return self.winner is not None

    @property
    def status(self) -> str:
        if self.insufficient_data:
            return "insufficient_data"
        if self.low_confidence:
            return "low_confidence"
        return "winner" if self.winner else "no_separation"


@dataclass
class Ranker:
    """Ranks each dimension independently, per channel.

    Args:
        min_samples: usable measurements required before any verdict is attempted.
        top_k / bottom_k: how many example excerpts travel forward in the signal.
        excerpt_chars: how much of each example is carried. Enough to recognize a
            pattern, not so much that the brief becomes the corpus.
    """

    min_samples: int = 3
    top_k: int = 2
    bottom_k: int = 1
    excerpt_chars: int = 240

    @classmethod
    def from_config(cls, cfg: dict) -> Ranker:
        return cls(
            min_samples=int(cfg.get("min_samples", 3)),
            top_k=int(cfg.get("top_k", 2)),
            bottom_k=int(cfg.get("bottom_k", 1)),
            excerpt_chars=int(cfg.get("excerpt_chars", 240)),
        )

    def usable(self, samples: list[Sample], channel: str) -> list[Sample]:
        """Samples on this channel with a real, finite score.

        None is dropped rather than coerced: see ABSENT IS NOT ZERO above.
        """
        return [
            s
            for s in samples
            if s.channel == channel
            and s.score is not None
            and math.isfinite(s.score)
        ]

    def rank_dimension(
        self, samples: list[Sample], *, channel: str, dimension: str
    ) -> Learnings:
        usable = [s for s in self.usable(samples, channel) if dimension in s.tags]

        if len(usable) < self.min_samples:
            return Learnings(
                channel=channel,
                dimension=dimension,
                winner=None,
                means={},
                sample_count=len(usable),
                insufficient_data=True,
            )

        groups: dict[str, list[float]] = {}
        for s in usable:
            groups.setdefault(s.tags[dimension], []).append(float(s.score))  # type: ignore[arg-type]
        means = {k: sum(v) / len(v) for k, v in groups.items()}

        if len(means) < 2:
            # Every sample used the same value. Nothing was compared to anything.
            return Learnings(
                channel=channel,
                dimension=dimension,
                winner=None,
                means=means,
                sample_count=len(usable),
                low_confidence=True,
            )

        ordered = sorted(means.items(), key=lambda kv: (-kv[1], kv[0]))
        (top_val, top_mean), (_, second_mean) = ordered[0], ordered[1]

        if top_mean - second_mean <= MIN_SEPARATION:
            # A tie at the top. Distinct from `low_confidence`: here two or more values
            # WERE genuinely compared and came out level, which is a real finding ("these
            # options are equivalent so far"), whereas low_confidence means nothing was
            # compared to anything. Naming either side would be a coin flip presented as
            # a finding, and the loop would then chase it for every future round.
            return Learnings(
                channel=channel,
                dimension=dimension,
                winner=None,
                means=means,
                sample_count=len(usable),
            )

        return Learnings(
            channel=channel,
            dimension=dimension,
            winner=top_val,
            means=means,
            sample_count=len(usable),
        )

    def rank_all(
        self, samples: list[Sample], *, channel: str, dimensions: tuple[str, ...]
    ) -> dict[str, Learnings]:
        return {
            d: self.rank_dimension(samples, channel=channel, dimension=d) for d in dimensions
        }

    def signal(
        self,
        *,
        channel: str,
        round_index: int,
        samples: list[Sample],
        learnings: dict[str, Learnings],
    ) -> LearningSignal:
        """Bundle the dimension verdicts plus bounded examples for the next round."""
        usable = sorted(
            self.usable(samples, channel),
            key=lambda s: (-float(s.score), s.variant_id),  # type: ignore[arg-type]
        )

        guidance: list[str] = []
        for dim, learn in sorted(learnings.items()):
            if learn.actionable:
                guidance.append(
                    f"{dim}: {learn.winner!r} scored highest over {learn.sample_count} "
                    f"samples on {channel}; this is a correlation on a small sample, "
                    f"not a measured causal lift"
                )
            else:
                guidance.append(
                    f"{dim}: no winner ({learn.status}, {learn.sample_count} samples); "
                    f"keep rotating this dimension"
                )

        top = tuple(self._excerpt(s.excerpt) for s in usable[: self.top_k] if s.excerpt)
        weak = tuple(
            self._excerpt(s.excerpt)
            for s in reversed(usable[-self.bottom_k :])
            if s.excerpt
        )
        return LearningSignal(
            channel=channel,
            round_index=round_index,
            top_examples=top,
            weak_examples=weak,
            guidance=tuple(guidance),
        )

    def _excerpt(self, text: str) -> str:
        t = " ".join(text.split())
        if len(t) <= self.excerpt_chars:
            return t
        return t[: self.excerpt_chars].rstrip() + "..."


@dataclass
class Tilt:
    """Applies dimension winners to the next round's style, except when exploring.

    `style_for_round` starts from the plain rotation and overrides only the dimensions
    that produced a real winner. On an exploration position it ignores every winner and
    returns the plain rotation, which is what keeps the space covered.
    """

    exploration_fraction: float = 0.34
    _applied: dict[str, str] = field(default_factory=dict)

    def style_for_round(self, space, position: int, learnings: dict[str, Learnings]):
        base = space.rotation_at(position, 1)[0]
        if space.is_exploration(position, self.exploration_fraction):
            self._applied = {}
            return base, True

        coords = list(base.coords)
        applied: dict[str, str] = {}
        for i, (axis_name, _current) in enumerate(coords):
            learn = learnings.get(axis_name)
            if learn is None or not learn.actionable:
                continue
            winner = learn.winner
            # Fail closed on a winner that is not a real value of this axis: a stale
            # persisted learning must never be able to inject an unknown coordinate.
            axis = next((a for a in space.axes if a.name == axis_name), None)
            if axis is None or winner not in axis.values:
                continue
            coords[i] = (axis_name, winner)
            applied[axis_name] = winner
        self._applied = applied

        from ..generation.style import Style

        return Style(coords=tuple(coords)), False

    @property
    def applied_winners(self) -> dict[str, str]:
        """Only the winners that were actually applied, for an honest provenance record."""
        return dict(self._applied)
