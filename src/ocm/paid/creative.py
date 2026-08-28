"""Resolving a creative reference to actual bytes, and proving they are the approved bytes.

THE BUG THIS MODULE EXISTS TO PREVENT

A creative ref in config was once resolved with a bare `Path(ref)`. That resolves against
the process working directory. So the same config answered "does this creative exist and
does it match?" differently depending on which directory the command was run from. A gate
on real ad spend whose verdict depended on the caller's shell.

The rules are therefore explicit and every ambiguous case is REFUSED rather than guessed:

    absolute ref                      taken exactly as written
    relative ref + absolute base      base / ref
    relative ref + no base            refused (BaseDirUnknown)
    relative ref + relative base      refused (same defect, moved one argument upstream)

VALIDATION IS A RE-HASH, AND IT HAPPENS BEFORE THE FIRST WRITE

`load_creatives` reads and re-hashes EVERY creative before any of them is uploaded. If a
file on disk no longer hashes to the value in config, it is not the asset that was
approved, and nothing is created. The ordering is the point: validating creative 3 after
creatives 1 and 2 have already been uploaded leaves a half-built campaign holding assets
nobody approved.

ONE READ, THREADED THROUGH

Each creative is read exactly once and the result is carried in a `CreativeRead`. Every
downstream surface (the review card, the uploader, the error report) renders from that one
read. Reading twice is a real bug and not a theoretical one: between two reads a symlink
can be repointed, so the card can print the location from the first read next to a verdict
earned by the second read's bytes.

WHAT IS DELIBERATELY NOT ENFORCED, AND WHY

Path containment is not enforced. `..`, symlinks, and refs outside the campaign directory
are all allowed. Reaching any of them requires write access to the campaign config, and
whoever has that can change the landing page and the budget instead, so containment would
buy nothing against that attacker. The honest residue is recorded rather than papered over:
a mutable symlink inside a shared creative directory needs no config access at all, and the
mitigation is only that the mismatch is caught before any upload, not that it cannot occur.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .campaign import CreativeRef


class CreativeError(Exception):
    """Base for creative resolution and validation failures."""


class BaseDirUnknown(CreativeError, ValueError):
    """A relative ref was given with no usable base directory.

    Its own type, so a review-card renderer can report an unmet item while an uploader
    still refuses outright. Both are correct responses; they are not the same response.
    """


class CreativeHashMismatch(CreativeError):
    """The file on disk is not the asset that was approved."""


def resolve(ref: str, base_dir: str | None) -> Path:
    """Turn a config ref into an absolute path, or refuse."""
    p = Path(ref)
    if p.is_absolute():
        return p
    if not base_dir:
        raise BaseDirUnknown(
            f"relative creative ref {ref!r} has no campaign directory to resolve against; "
            f"refusing to guess against the process working directory"
        )
    base = Path(base_dir)
    if not base.is_absolute():
        raise BaseDirUnknown(
            f"campaign directory {base_dir!r} is itself relative; the same config would "
            f"resolve differently from different working directories"
        )
    return base / p


def hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class CreativeRead:
    """The single read of one creative. Every surface renders from this."""

    ref: str
    approved_hash: str
    resolved: Path | None
    opened: Path | None
    payload: bytes | None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.error == "" and self.payload is not None

    @property
    def computed_hash(self) -> str | None:
        return hash_bytes(self.payload) if self.payload is not None else None


def read_creative(ref: CreativeRef, base_dir: str | None) -> CreativeRead:
    """Resolve, read, and re-hash one creative. Never raises for a bad file.

    Errors are captured into the record rather than raised, so a card renderer can list
    every unmet creative at once instead of reporting only the first.
    """
    try:
        resolved = resolve(ref.ref, base_dir)
    except BaseDirUnknown as exc:
        # Deliberately outside the read try-block so this error text survives intact.
        return CreativeRead(ref.ref, ref.content_hash, None, None, None, str(exc))

    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        return CreativeRead(ref.ref, ref.content_hash, resolved, None, None, f"unreadable: {exc}")
    except ValueError as exc:
        # A NUL byte inside a ref raises ValueError, not OSError.
        return CreativeRead(ref.ref, ref.content_hash, resolved, None, None, f"invalid path: {exc}")

    opened = Path(os.path.realpath(resolved))
    computed = hash_bytes(payload)
    if computed != ref.content_hash:
        return CreativeRead(
            ref.ref,
            ref.content_hash,
            resolved,
            opened,
            None,
            # The APPROVED hash is named; the computed one is not echoed. The computed
            # digest is a fact about bytes the reviewer did not approve, and printing it
            # onto a human-facing surface is how an unapproved asset gets legitimized.
            "content hash mismatch: the file on disk is not the asset that was approved",
        )
    return CreativeRead(ref.ref, ref.content_hash, resolved, opened, payload)


def load_creatives(
    refs: tuple[CreativeRef, ...], base_dir: str | None
) -> tuple[list[CreativeRead], list[str]]:
    """Read and validate every creative. Returns (reads, errors).

    Callers on the spend path must treat a non-empty error list as fatal BEFORE creating
    anything. `assert_all_ok` is the one-line form.
    """
    reads = [read_creative(r, base_dir) for r in refs]
    errors = [f"{r.ref}: {r.error}" for r in reads if not r.ok]
    return reads, errors


def assert_all_ok(reads: list[CreativeRead]) -> None:
    errors = [f"{r.ref}: {r.error}" for r in reads if not r.ok]
    if errors:
        raise CreativeHashMismatch(
            "refusing to create anything; unmet creatives: " + "; ".join(errors)
        )
