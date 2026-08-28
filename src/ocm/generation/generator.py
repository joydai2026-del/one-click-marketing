"""Content generation, and the grounding rules around it.

Generation itself is one call to a model. Everything interesting is the frame around it:

GROUNDING. The generator is handed a fact bank and is instructed to use only facts from it.
An always-on marketing loop that invents a statistic is a compliance incident, not a bad
post. The compliance floor independently re-checks for numeric claims that do not trace to
a fact, so an invented figure is caught even when the prompt is ignored.

SELF-CORRECTION WITH A BUDGET. A rejected draft is regenerated with the rejection reason
appended to the prompt, up to a fixed attempt budget. Exhausting the budget raises. It does
NOT return the least-bad attempt: a loop that degrades under pressure publishes its worst
output at exactly the moment something is wrong.

THE AVOID BLOCK IS MASKED. Recent posts are shown to the generator so it does not repeat
itself. But if a forbidden term appears in the generator's own history, showing that history
hands the model the exact string it was just told never to write. So forbidden terms are
masked inside the avoid block before it is shown.

`TemplateGenerator` is the dry-run implementation. It is deterministic, needs no API key,
and every string it produces is visibly fabricated placeholder text. It is not a writing
model and the repository never pretends it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..evaluation.compliance import ComplianceFloor, normalize
from .style import Style


class GenerationError(Exception):
    """Raised when the attempt budget is exhausted without a usable draft."""


@dataclass(frozen=True)
class FactBank:
    """The only material a draft may assert. Loaded from config, never from the model."""

    topics: tuple[str, ...]
    facts: tuple[str, ...]
    voice: str = ""

    @classmethod
    def from_config(cls, cfg: dict) -> FactBank:
        topics = tuple(cfg.get("topics", []))
        facts = tuple(cfg.get("facts", []))
        if not topics:
            raise ValueError("fact bank defines no topics")
        if not facts:
            raise ValueError("fact bank defines no facts: nothing could be grounded")
        return cls(topics=topics, facts=facts, voice=str(cfg.get("voice", "")))


@dataclass(frozen=True)
class GenerationRequest:
    style: Style
    topic: str
    channel: str
    bank: FactBank
    avoid: tuple[str, ...] = ()
    guidance: tuple[str, ...] = ()
    correction: str = ""
    # The channel's own length band, passed in rather than looked up, so the generator
    # never needs to know which channels exist. Stating the band in the prompt is also
    # what stops the generator producing long-form copy for a short-form field and then
    # failing validation on every retry.
    min_chars: int = 0
    max_chars: int | None = None
    needs_title: bool = False


class Model(Protocol):
    """The one call this repository would make to a language model."""

    def complete(self, prompt: str) -> str: ...


def mask_terms(text: str, terms: list[str]) -> str:
    """Replace forbidden terms with a placeholder before showing text back to a model.

    Case-insensitive, working on the original string so surrounding text is preserved.
    """
    out = text
    for term in terms:
        if not term:
            continue
        lowered = out.lower()
        needle = term.lower()
        start = 0
        pieces: list[str] = []
        while True:
            idx = lowered.find(needle, start)
            if idx == -1:
                pieces.append(out[start:])
                break
            pieces.append(out[start:idx])
            pieces.append("[redacted]")
            start = idx + len(needle)
        out = "".join(pieces)
    return out


def build_prompt(req: GenerationRequest, forbidden_terms: list[str]) -> str:
    """Assemble the generation prompt. Pure and testable: it makes no model call."""
    band = f"between {req.min_chars} and {req.max_chars} characters" if req.max_chars else (
        f"at least {req.min_chars} characters"
    )
    lines: list[str] = [
        f"Write one piece of content for the {req.channel} channel.",
        f"Length: {band}",
        f"Topic: {req.topic}",
        "Style:",
    ]
    for axis, value in req.style.coords:
        lines.append(f"  - {axis}: {value}")
    if req.bank.voice:
        lines.append(f"Voice: {req.bank.voice}")
    lines.append("")
    lines.append("Use ONLY these facts. Do not introduce any number or claim not listed:")
    lines.extend(f"  - {f}" for f in req.bank.facts)
    if forbidden_terms:
        lines.append("")
        lines.append(
            "Never write any of the following, including as an abbreviation, an "
            "initialism, or inside a domain name:"
        )
        lines.extend(f"  - {t}" for t in forbidden_terms)
    if req.guidance:
        lines.append("")
        lines.append("Signals from the previous round:")
        lines.extend(f"  - {g}" for g in req.guidance)
    if req.avoid:
        lines.append("")
        lines.append("Do not repeat these recent pieces:")
        for a in req.avoid:
            lines.append(f"  - {mask_terms(a, forbidden_terms)}")
    if req.correction:
        lines.append("")
        lines.append(f"The previous attempt was rejected: {req.correction}. Fix that.")
    return "\n".join(lines)


@dataclass
class Generator:
    """Wraps a model with grounding, a compliance preflight, and a retry budget."""

    model: Model
    compliance: ComplianceFloor
    max_attempts: int = 3
    avoid_similarity: float = 0.6

    def generate(self, req: GenerationRequest) -> str:
        correction = req.correction
        last_reason = "no attempt was made"
        for _ in range(max(1, self.max_attempts)):
            attempt_req = GenerationRequest(
                style=req.style,
                topic=req.topic,
                channel=req.channel,
                bank=req.bank,
                avoid=req.avoid,
                guidance=req.guidance,
                correction=correction,
                min_chars=req.min_chars,
                max_chars=req.max_chars,
                needs_title=req.needs_title,
            )
            prompt = build_prompt(attempt_req, self.compliance.forbidden_terms)
            text = self.model.complete(prompt).strip()

            violations = self.compliance.check(text)
            if violations:
                last_reason = "; ".join(str(v) for v in violations)
                correction = last_reason
                continue

            repeat = self._too_similar(text, req.avoid)
            if repeat is not None:
                last_reason = f"too similar to a recent piece ({repeat:.2f})"
                correction = last_reason
                continue

            return text

        # Fail closed. Returning the least-bad attempt would publish the worst output at
        # exactly the moment the generator is malfunctioning.
        raise GenerationError(
            f"exhausted {self.max_attempts} attempts; last rejection: {last_reason}"
        )

    def _too_similar(self, text: str, avoid: tuple[str, ...]) -> float | None:
        from ..evaluation.dedup import similarity

        for prior in avoid:
            s = similarity(text, prior)
            if s >= self.avoid_similarity:
                return s
        return None


@dataclass
class TemplateGenerator:
    """Deterministic, offline stand-in for a language model.

    Produces obviously-fabricated placeholder text from the style, topic, fact bank, and
    the channel's length band as stated in the prompt. It exists so the loop's control
    flow is exercisable with no API key, and so the demo output can never be mistaken for
    real marketing copy: every piece it writes says so in its own text.

    It is not a writing model, makes no claim to be one, and the quality of its output is
    not evidence about the quality of the pipeline's real output.
    """

    _calls: list[str] = field(default_factory=list)

    def complete(self, prompt: str) -> str:
        self._calls.append(prompt)
        topic = _extract(prompt, "Topic: ")
        channel = _extract_channel(prompt)
        min_chars, max_chars = _extract_band(prompt)
        style_bits = _style_bits(prompt)
        facts = _facts_from(prompt)

        head = f"[SAMPLE CONTENT for {channel}] {topic}"
        style_line = ("Style: " + ", ".join(style_bits)) if style_bits else ""
        # Which facts get cited, and in what order, is rotated by a stable hash of the
        # topic and style. Without this every sample cites the same facts in the same
        # order, the shared boilerplate dominates the shingle set, and the dedup guard
        # rejects everything after the first draft. That would be an artifact of the stub,
        # not a finding about the guard.
        body = " ".join(
            f"Placeholder line {n + 1} citing: {f}."
            for n, f in enumerate(_rotate(facts, f"{topic}|{style_line}"))
        )
        tail = (
            "Placeholder output from a deterministic offline generator in a demonstration "
            "repository. Not marketing copy."
        )
        text = "\n\n".join(p for p in (head, style_line, body, tail) if p)

        if max_chars is not None and len(text) > max_chars:
            return _fit(f"{head} | {', '.join(style_bits)} | placeholder sample", max_chars)
        # Padding paragraphs vary by index, topic, and style so that two long-form samples
        # on different topics are not near-duplicates of each other. Identical filler
        # would dominate the shingle set and the dedup guard would reject every long-form
        # draft after the first, which says nothing about the guard and everything about
        # the stub.
        i = 0
        while len(text) < min_chars:
            i += 1
            filler = (
                f"Placeholder paragraph {i} for the {channel} channel on the subject of "
                f"{topic}, written in the {'/'.join(style_bits) or 'default'} style, "
                f"included so this sample clears the configured minimum length. It makes "
                f"no claim and cites nothing beyond the fact bank above."
            )
            candidate = text + "\n\n" + filler
            if max_chars is not None and len(candidate) > max_chars:
                break
            text = candidate
        return text


def _extract(prompt: str, marker: str) -> str:
    for line in prompt.splitlines():
        if line.startswith(marker):
            return line[len(marker) :].strip()
    return "untitled"


def _extract_channel(prompt: str) -> str:
    for line in prompt.splitlines():
        if line.startswith("Write one piece of content for the "):
            return line.split("for the ", 1)[1].replace(" channel.", "").strip()
    return "unknown"


def _extract_band(prompt: str) -> tuple[int, int | None]:
    for line in prompt.splitlines():
        if not line.startswith("Length: "):
            continue
        nums = [int(t) for t in line.replace(",", " ").split() if t.isdigit()]
        if "between" in line and len(nums) >= 2:
            return nums[0], nums[1]
        if nums:
            return nums[0], None
    return 0, None


def _style_bits(prompt: str) -> list[str]:
    out: list[str] = []
    collecting = False
    for line in prompt.splitlines():
        if line.startswith("Style:"):
            collecting = True
            continue
        if collecting:
            if line.startswith("  - "):
                out.append(line[4:].strip())
            else:
                break
    return out


def _rotate(items: list[str], seed: str) -> list[str]:
    """Deterministic rotation of a list, keyed by a seed string."""
    if not items:
        return []
    import hashlib

    n = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) % len(items)
    return items[n:] + items[:n]


def _fit(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip()


def _facts_from(prompt: str) -> list[str]:
    out: list[str] = []
    collecting = False
    for line in prompt.splitlines():
        if line.startswith("Use ONLY these facts"):
            collecting = True
            continue
        if collecting:
            if line.startswith("  - "):
                out.append(line[4:].strip())
            elif line.strip() == "":
                continue
            else:
                break
    return out


def normalized_equal(a: str, b: str) -> bool:
    """Cross-channel identical-text check, used to hold rather than to regenerate."""
    return normalize(a) == normalize(b)
