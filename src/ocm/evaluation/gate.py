"""The quality gate: the single place a draft is allowed to become publishable.

Order of operations matters and is deliberate:

    1. compliance floors   cheap, deterministic, non-compensable
    2. dedup               cheap, deterministic, non-compensable
    3. rubric scoring      expensive (an LLM call in production), compensable

Steps 1 and 2 short-circuit. There is no point paying a judge to grade a draft that is
already disqualified, and there is no threshold high enough to make a compliance failure
acceptable.

The gate returns an `EvalResult` and never publishes anything itself. Publication requires
a separate human approval bound to `EvalResult.content_hash`. Keeping "is it good enough"
and "may it go out" as two different questions is what stops a quality improvement from
silently becoming a permission to spend.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Draft, EvalResult, Verdict
from .compliance import ComplianceFloor
from .dedup import DedupIndex
from .rubric import Rubric, Scorer


@dataclass
class QualityGate:
    rubric: Rubric
    compliance: ComplianceFloor
    dedup: DedupIndex
    scorer: Scorer

    def evaluate(self, draft: Draft) -> EvalResult:
        text = f"{draft.title}\n{draft.body}".strip()
        chash = draft.content_hash

        hard: list[str] = []
        notes: list[str] = []

        for v in self.compliance.check(text):
            hard.append(str(v))

        dup = self.dedup.duplicate_of(text, chash)
        if dup is not None:
            ref, score = dup
            hard.append(f"duplicate: {score:.2f} similar to {ref}")

        if hard:
            # Short-circuit: do not spend a judge call on a disqualified draft.
            return EvalResult(
                draft_id=draft.draft_id,
                content_hash=chash,
                verdict=Verdict.FAIL,
                weighted_score=0.0,
                threshold=self.rubric.threshold,
                dimensions=(),
                hard_failures=tuple(hard),
                notes=("rubric not scored: hard floor tripped first",),
            )

        weighted, dims, floor_failures = self.rubric.score(text, self.scorer)
        hard.extend(floor_failures)

        below_threshold = weighted < self.rubric.threshold
        if below_threshold:
            notes.append(
                f"weighted score {weighted:.2f} below threshold {self.rubric.threshold:.2f}"
            )

        verdict = Verdict.FAIL if (hard or below_threshold) else Verdict.PASS
        return EvalResult(
            draft_id=draft.draft_id,
            content_hash=chash,
            verdict=verdict,
            weighted_score=weighted,
            threshold=self.rubric.threshold,
            dimensions=tuple(dims),
            hard_failures=tuple(hard),
            notes=tuple(notes),
        )
