"""Hard compliance floors.

A floor is different from a rubric dimension. A rubric dimension contributes a weighted
score and can be compensated for by other dimensions. A floor cannot: if it trips, the
draft is dead no matter how good the rest of it is.

Two floor families ship here, both entirely config-driven:

1. Forbidden-substring compliance. The operator supplies a list of terms that must never
   appear in published content. In the production system this list encodes legal and
   brand constraints; here it is a generic mechanism with a placeholder list. Matching is
   case-insensitive and whitespace-normalized so that trivial evasion (casing, doubled
   spaces, zero-width characters) does not slip through.

2. Structural rules. Length bounds, a maximum outbound-link count, and required or
   forbidden markers. These are the rules that are cheap to state exactly and therefore
   should never be delegated to a language model's judgment.

Design note: floors are checked BEFORE the model-scored rubric, because they are cheap
and deterministic, and because a floor failure makes the rubric call a waste of money.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Characters that render as nothing but break naive substring matching.
#
# ENUMERATING THEM BY HAND DOES NOT WORK. This started as a list of six well-known
# offenders and was evaded by U+200E LEFT-TO-RIGHT MARK, which was simply not on it. Any
# hand-written list is a list of the invisible characters someone happened to think of,
# and the attacker only has to find one that was forgotten.
#
# So the rule is CATEGORICAL: strip every character Unicode itself classifies as a format
# character (category Cf) or a control character (Cc), plus the handful of spacing marks
# that render as nothing. That covers the zero-widths, the bidi marks, the soft hyphen,
# and anything added to the standard in future, without needing to know their names.
_STRIP_CATEGORIES = frozenset({"Cf", "Cc"})
_EXTRA_INVISIBLE = frozenset(
    {
        0x00AD,  # soft hyphen (category Pd, so not caught by the rule above)
        0x115F,  # hangul choseong filler
        0x1160,  # hangul jungseong filler
        0x3164,  # hangul filler
        0xFFA0,  # halfwidth hangul filler
    }
)


def _strip_invisible(text: str) -> str:
    return "".join(
        ch
        for ch in text
        if ord(ch) not in _EXTRA_INVISIBLE
        and unicodedata.category(ch) not in _STRIP_CATEGORIES
    )

_LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def normalize(text: str) -> str:
    """Fold text into a form where cosmetic evasion of a term list does not work.

    NFKC-folds unicode lookalikes, strips invisible characters, lowercases, and collapses
    all runs of whitespace to a single space.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _strip_invisible(text)
    text = text.lower()
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class ComplianceViolation:
    rule: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.rule}: {self.detail}"


@dataclass
class ComplianceFloor:
    """Deterministic pass/fail checks applied to every draft on every channel.

    Args:
        forbidden_terms: substrings that must not appear. Reported by index, never echoed
            back into logs, so that a sensitive term list does not leak through the audit
            trail of a system that exists to keep it out of public content.
        max_links: maximum number of outbound URLs allowed. 0 enforces a no-links policy.
        min_chars / max_chars: length bounds for the body.
        required_markers: substrings that must be present (for example a disclosure label).
    """

    forbidden_terms: list[str] = field(default_factory=list)
    max_links: int | None = None
    min_chars: int = 1
    max_chars: int | None = None
    required_markers: list[str] = field(default_factory=list)
    # When True, violations name the matched term. Off by default: the term list is the
    # sensitive artifact, and an audit log is a wider surface than the config file.
    echo_terms: bool = False

    @classmethod
    def from_config(cls, cfg: dict) -> ComplianceFloor:
        return cls(
            forbidden_terms=list(cfg.get("forbidden_terms", [])),
            max_links=cfg.get("max_links"),
            min_chars=int(cfg.get("min_chars", 1)),
            max_chars=cfg.get("max_chars"),
            required_markers=list(cfg.get("required_markers", [])),
            echo_terms=bool(cfg.get("echo_terms", False)),
        )

    def check(self, text: str) -> list[ComplianceViolation]:
        violations: list[ComplianceViolation] = []
        norm = normalize(text)

        for i, term in enumerate(self.forbidden_terms):
            term_norm = normalize(term)
            if term_norm and term_norm in norm:
                detail = f"matched term #{i}" if not self.echo_terms else f"matched {term!r}"
                violations.append(ComplianceViolation("forbidden_term", detail))

        if self.max_links is not None:
            n = len(_LINK_RE.findall(text))
            if n > self.max_links:
                violations.append(
                    ComplianceViolation("max_links", f"{n} links found, limit is {self.max_links}")
                )

        n_chars = len(text.strip())
        if n_chars < self.min_chars:
            violations.append(
                ComplianceViolation("min_chars", f"{n_chars} chars, minimum is {self.min_chars}")
            )
        if self.max_chars is not None and n_chars > self.max_chars:
            violations.append(
                ComplianceViolation("max_chars", f"{n_chars} chars, maximum is {self.max_chars}")
            )

        for marker in self.required_markers:
            if normalize(marker) not in norm:
                violations.append(
                    ComplianceViolation("required_marker", f"missing marker {marker!r}")
                )

        return violations
