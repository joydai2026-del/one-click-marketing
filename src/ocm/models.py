"""Core data structures that flow between loop stages.

These are deliberately plain dataclasses with explicit serialization. Every record that
crosses a stage boundary carries a `content_hash`, because the approval gate binds a human
decision to a hash and not to a database row that could be edited afterwards.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


def content_hash(*parts: str) -> str:
    """Stable content fingerprint used everywhere a decision is bound to content.

    Parts are length-prefixed before hashing so that ("ab", "c") and ("a", "bc")
    cannot collide.
    """
    h = hashlib.sha256()
    for p in parts:
        raw = p.encode("utf-8")
        h.update(str(len(raw)).encode("ascii"))
        h.update(b":")
        h.update(raw)
    return h.hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def now_ts() -> float:
    return time.time()


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class Stage(StrEnum):
    DRAFTED = "drafted"
    EVALUATED = "evaluated"
    APPROVED = "approved"
    PUBLISHED = "published"
    COLLECTED = "collected"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Draft:
    """A candidate piece of content, before any gate has looked at it."""

    draft_id: str
    channel: str
    body: str
    title: str = ""
    media_paths: tuple[str, ...] = ()
    # Which learning signals shaped this draft. Kept for auditability of the loop.
    derived_from: tuple[str, ...] = ()
    created_at: float = field(default_factory=now_ts)

    @property
    def content_hash(self) -> str:
        return content_hash(self.channel, self.title, self.body, "|".join(self.media_paths))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["media_paths"] = list(self.media_paths)
        d["derived_from"] = list(self.derived_from)
        d["content_hash"] = self.content_hash
        return d


@dataclass(frozen=True)
class DimensionScore:
    name: str
    score: float
    weight: float
    note: str = ""


@dataclass(frozen=True)
class EvalResult:
    """Output of the quality gate for one draft.

    `verdict` is PASS only when every hard floor passed AND the weighted rubric score
    cleared the configured threshold. A single hard-floor failure is fatal regardless of
    how high the rubric score is: that is the point of a floor.
    """

    draft_id: str
    content_hash: str
    verdict: Verdict
    weighted_score: float
    threshold: float
    dimensions: tuple[DimensionScore, ...] = ()
    hard_failures: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.verdict is Verdict.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "content_hash": self.content_hash,
            "verdict": self.verdict.value,
            "weighted_score": round(self.weighted_score, 4),
            "threshold": self.threshold,
            "dimensions": [asdict(d) for d in self.dimensions],
            "hard_failures": list(self.hard_failures),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class PublishRecord:
    """Proof that one draft reached one channel exactly once."""

    publish_id: str
    draft_id: str
    channel: str
    content_hash: str
    external_id: str
    external_url: str
    dry_run: bool
    published_at: float = field(default_factory=now_ts)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EngagementRecord:
    """Raw, per-channel engagement counters pulled back after publication.

    Channels do not agree on what a "view" is, so nothing here is comparable across
    channels until `learning.scoring` normalizes it.
    """

    publish_id: str
    channel: str
    collected_at: float
    metrics: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LearningSignal:
    """What the next generation round is told about the last one.

    Deliberately small. A signal is an instruction to the generator, not a dump of the
    whole history, so that a bad round cannot poison an unbounded amount of future work.
    """

    channel: str
    round_index: int
    top_examples: tuple[str, ...] = ()
    weak_examples: tuple[str, ...] = ()
    guidance: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["top_examples"] = list(self.top_examples)
        d["weak_examples"] = list(self.weak_examples)
        d["guidance"] = list(self.guidance)
        return d


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)
