"""Deterministic identity for a generated item.

THE PROBLEM

Generation is non-deterministic. Ask a model for a post about topic T in style S twice and
you get two different texts. If the identity of a work item is derived from that text, then
a crash-and-retry produces a NEW id, the store does not recognize it, and the pipeline
stages and publishes a second copy of something a human already saw. That is a duplicate
post, or on the paid side a duplicate campaign and a double charge.

THE FIX

Identity is derived from the SLOT INPUTS, never from the generated output:

    slot_id    = sha256(style_id | topic)[:12]
    variant_id = f"{run_id}-{channel}-{slot_id}"

Re-run the same tick and every id is identical, so the store's uniqueness check does the
deduplication for free. The generated text is free to differ; the work item is the same
work item.

The corollary is that a slot collision must be LOUD. If two slots in one run compute the
same id, one would silently overwrite the other and a piece of planned content would vanish
without any error. `assert_unique_slots` raises instead.
"""

from __future__ import annotations

import hashlib

SLOT_ID_LEN = 12


def slot_id(style_id: str, topic: str) -> str:
    """Stable id for one (style, topic) pair.

    Fields are NUL-separated so that ("a.b", "c") and ("a", "b.c") cannot collide.
    """
    material = f"{style_id}\x00{topic}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:SLOT_ID_LEN]


def variant_id(run_id: str, channel: str, slot: str) -> str:
    return f"{run_id}-{channel}-{slot}"


def assert_unique_slots(slots: list[tuple[str, str]]) -> None:
    """Raise if any (style_id, topic) pair repeats within one run.

    Loud, because the alternative is one planned item silently disappearing.
    """
    seen: dict[str, tuple[str, str]] = {}
    for style, topic in slots:
        sid = slot_id(style, topic)
        if sid in seen:
            raise ValueError(
                f"slot collision: {(style, topic)} and {seen[sid]} both map to {sid}"
            )
        seen[sid] = (style, topic)
