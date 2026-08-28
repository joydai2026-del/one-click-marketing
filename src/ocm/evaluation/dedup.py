"""Near-duplicate rejection.

An always-on generator drifts toward its own greatest hits. Left alone it will re-post a
paraphrase of last week's best performer, which reads as spam to humans and as duplicate
content to platforms. Exact-hash dedup does not catch this, because one changed word makes
a new hash.

The mechanism here is a shingle-based Jaccard similarity against the published corpus,
which is dependency-free, deterministic, and good enough at the scale a single account
publishes at. At corpus sizes where an O(n) scan stops being acceptable, the same
similarity function goes behind a MinHash or embedding index without any caller change.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .compliance import normalize


def shingles(text: str, k: int = 5) -> set[str]:
    """Word-level k-shingles of normalized text.

    Word shingles rather than character shingles: character shingles over-report
    similarity for two texts that merely share a topic vocabulary.
    """
    words = normalize(text).split()
    if not words:
        return set()
    if len(words) <= k:
        return {" ".join(words)}
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def similarity(a: str, b: str, k: int = 5) -> float:
    """Jaccard similarity of the two texts' shingle sets, in [0.0, 1.0]."""
    sa, sb = shingles(a, k), shingles(b, k)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


@dataclass
class DedupIndex:
    """Remembers what has already been published so the loop cannot repeat itself.

    Two guards, because they fail differently:
      - exact hash match, which catches a retry or a double-dispatch;
      - similarity above `threshold`, which catches the generator paraphrasing itself.
    """

    threshold: float = 0.6
    k: int = 5
    _hashes: set[str] = field(default_factory=set)
    _corpus: list[tuple[str, str]] = field(default_factory=list)  # (ref, text)

    @classmethod
    def from_config(cls, cfg: dict) -> DedupIndex:
        return cls(threshold=float(cfg.get("threshold", 0.6)), k=int(cfg.get("shingle_k", 5)))

    def add(self, ref: str, text: str, content_hash: str) -> None:
        self._hashes.add(content_hash)
        self._corpus.append((ref, text))

    def duplicate_of(self, text: str, content_hash: str) -> tuple[str, float] | None:
        """Return (matching_ref, score) if this text is a duplicate, else None."""
        if content_hash in self._hashes:
            return ("exact-hash", 1.0)
        best_ref, best = "", 0.0
        for ref, prior in self._corpus:
            s = similarity(text, prior, self.k)
            if s > best:
                best_ref, best = ref, s
        if best >= self.threshold:
            return (best_ref, best)
        return None

    def __len__(self) -> int:
        return len(self._corpus)
