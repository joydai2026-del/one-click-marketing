"""Creative resolution and re-hashing.

The bug this module exists to prevent: a creative ref resolved with a bare `Path(ref)`
resolves against the PROCESS WORKING DIRECTORY, so the same config answers "does this
creative match?" differently depending on which directory the command was run from. A gate
on real ad spend whose verdict depended on the caller's shell.
"""

from __future__ import annotations

import hashlib

import pytest

from ocm.paid.campaign import CreativeRef
from ocm.paid.creative import (
    BaseDirUnknown,
    CreativeError,
    CreativeHashMismatch,
    assert_all_ok,
    hash_bytes,
    load_creatives,
    read_creative,
    resolve,
)

PAYLOAD_A = b"sample creative A: fabricated placeholder copy\n"
PAYLOAD_B = b"sample creative B: fabricated placeholder copy\n"


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@pytest.fixture
def creative_dir(tmp_path):
    """An absolute campaign directory holding two creatives with known hashes."""
    d = tmp_path / "campaign" / "creatives"
    d.mkdir(parents=True)
    (d / "a.txt").write_bytes(PAYLOAD_A)
    (d / "b.txt").write_bytes(PAYLOAD_B)
    return d


@pytest.fixture
def elsewhere(tmp_path):
    """A second, unrelated directory to chdir into. Its contents must never be reached."""
    d = tmp_path / "elsewhere"
    (d / "creatives").mkdir(parents=True)
    # A DECOY at the same relative path with different bytes. If resolution ever falls
    # back to the working directory, this file gets read and the hash check fails, which
    # is how the test detects the regression rather than passing by luck.
    (d / "creatives" / "a.txt").write_bytes(b"DECOY: not the approved asset\n")
    return d


# --------------------------------------------------------------------------------------
# resolve
# --------------------------------------------------------------------------------------


def test_an_absolute_ref_is_used_as_written(creative_dir):
    path = str(creative_dir / "a.txt")
    assert resolve(path, base_dir=None) == creative_dir / "a.txt"
    assert resolve(path, base_dir="/some/other/base") == creative_dir / "a.txt"


def test_a_relative_ref_resolves_against_an_absolute_base(creative_dir):
    assert resolve("creatives/a.txt", str(creative_dir.parent)) == creative_dir / "a.txt"


def test_a_relative_ref_with_no_base_is_refused(creative_dir):
    """Refused rather than guessed: guessing means resolving against whatever directory
    the process happened to be started in."""
    with pytest.raises(BaseDirUnknown, match="refusing to guess"):
        resolve("creatives/a.txt", base_dir=None)
    with pytest.raises(BaseDirUnknown):
        resolve("creatives/a.txt", base_dir="")


def test_a_relative_ref_with_a_relative_base_is_refused(creative_dir):
    """The same defect moved one argument upstream, so it gets the same refusal."""
    with pytest.raises(BaseDirUnknown, match="is itself relative"):
        resolve("creatives/a.txt", base_dir="config/example")


def test_base_dir_unknown_is_both_a_creative_error_and_a_value_error():
    """Its own type, so a card renderer can report an unmet item while an uploader still
    refuses outright. Both are correct responses; they are not the same response."""
    assert issubclass(BaseDirUnknown, CreativeError)
    assert issubclass(BaseDirUnknown, ValueError)


# --------------------------------------------------------------------------------------
# working-directory independence: the point of the module
# --------------------------------------------------------------------------------------


def test_resolution_does_not_depend_on_the_process_working_directory(
    creative_dir, elsewhere, monkeypatch
):
    """THE invariant. Same config, same answer, from any shell."""
    base = str(creative_dir.parent)
    expected = creative_dir / "a.txt"

    monkeypatch.chdir(creative_dir.parent)
    from_campaign_dir = resolve("creatives/a.txt", base)

    monkeypatch.chdir(elsewhere)
    from_elsewhere = resolve("creatives/a.txt", base)

    assert from_campaign_dir == from_elsewhere == expected


def test_reading_and_hashing_does_not_depend_on_the_working_directory(
    creative_dir, elsewhere, monkeypatch
):
    """And the verdict, not merely the path, is stable. `elsewhere` holds a decoy at the
    same relative path, so a working-directory fallback would read different bytes and
    the hash check would fail.
    """
    ref = CreativeRef(ref="creatives/a.txt", content_hash=sha(PAYLOAD_A))
    base = str(creative_dir.parent)

    monkeypatch.chdir(creative_dir.parent)
    inside = read_creative(ref, base)

    monkeypatch.chdir(elsewhere)
    outside = read_creative(ref, base)

    assert inside.ok and outside.ok
    assert inside.payload == outside.payload == PAYLOAD_A
    assert inside.resolved == outside.resolved


def test_an_absolute_ref_is_also_working_directory_independent(
    creative_dir, elsewhere, monkeypatch
):
    ref = CreativeRef(ref=str(creative_dir / "a.txt"), content_hash=sha(PAYLOAD_A))
    monkeypatch.chdir(elsewhere)
    assert read_creative(ref, base_dir=None).ok


# --------------------------------------------------------------------------------------
# read_creative
# --------------------------------------------------------------------------------------


def test_a_matching_creative_reads_ok_and_carries_its_bytes_once(creative_dir):
    """One read, threaded through: between two reads a symlink can be repointed, so the
    card could print the location from the first read next to a verdict earned by the
    second read's bytes."""
    ref = CreativeRef(ref="a.txt", content_hash=sha(PAYLOAD_A))
    read = read_creative(ref, str(creative_dir))

    assert read.ok is True
    assert read.error == ""
    assert read.payload == PAYLOAD_A
    assert read.computed_hash == ref.content_hash
    assert read.resolved == creative_dir / "a.txt"
    assert read.opened is not None


def test_a_hash_mismatch_is_captured_in_the_record_rather_than_raised(creative_dir):
    ref = CreativeRef(ref="a.txt", content_hash="0" * 64)
    read = read_creative(ref, str(creative_dir))

    assert read.ok is False
    assert "content hash mismatch" in read.error
    assert read.payload is None


def test_the_mismatch_message_does_not_echo_the_computed_hash(creative_dir):
    """The computed digest is a fact about bytes the reviewer did not approve, and
    printing it onto a human-facing surface is how an unapproved asset gets legitimized.
    """
    ref = CreativeRef(ref="a.txt", content_hash="0" * 64)
    read = read_creative(ref, str(creative_dir))

    assert sha(PAYLOAD_A) not in read.error
    assert sha(PAYLOAD_A)[:12] not in read.error
    # The APPROVED hash is still available on the record, which is what a reviewer needs.
    assert read.approved_hash == "0" * 64


def test_a_missing_file_is_captured_as_unreadable_not_raised(creative_dir):
    ref = CreativeRef(ref="does-not-exist.txt", content_hash=sha(PAYLOAD_A))
    read = read_creative(ref, str(creative_dir))
    assert read.ok is False
    assert "unreadable" in read.error


def test_a_relative_ref_with_no_base_is_captured_as_an_error_on_the_record():
    ref = CreativeRef(ref="a.txt", content_hash=sha(PAYLOAD_A))
    read = read_creative(ref, base_dir=None)
    assert read.ok is False
    assert "refusing to guess" in read.error
    assert read.resolved is None


def test_a_symlink_is_read_and_its_real_target_is_recorded(creative_dir):
    """`opened` records where the bytes actually came from, which is what makes a
    repointed symlink visible after the fact."""
    link = creative_dir / "link.txt"
    link.symlink_to(creative_dir / "a.txt")
    ref = CreativeRef(ref="link.txt", content_hash=sha(PAYLOAD_A))
    read = read_creative(ref, str(creative_dir))
    assert read.ok is True
    assert read.opened is not None and read.opened.name == "a.txt"


def test_hash_bytes_matches_sha256():
    assert hash_bytes(PAYLOAD_A) == sha(PAYLOAD_A)


# --------------------------------------------------------------------------------------
# load_creatives and assert_all_ok
# --------------------------------------------------------------------------------------


def test_load_creatives_reports_every_error_not_only_the_first(creative_dir):
    """Reporting one at a time turns a three-creative problem into three review cycles."""
    refs = (
        CreativeRef(ref="a.txt", content_hash="0" * 64),
        CreativeRef(ref="b.txt", content_hash="1" * 64),
        CreativeRef(ref="missing.txt", content_hash="2" * 64),
    )
    reads, errors = load_creatives(refs, str(creative_dir))

    assert len(reads) == 3
    assert len(errors) == 3
    assert {"a.txt", "b.txt", "missing.txt"} == {e.split(":")[0] for e in errors}


def test_load_creatives_returns_no_errors_when_every_asset_matches(creative_dir):
    refs = (
        CreativeRef(ref="a.txt", content_hash=sha(PAYLOAD_A)),
        CreativeRef(ref="b.txt", content_hash=sha(PAYLOAD_B)),
    )
    reads, errors = load_creatives(refs, str(creative_dir))
    assert errors == []
    assert all(r.ok for r in reads)


def test_load_creatives_reads_all_of_them_even_when_an_early_one_is_bad(creative_dir):
    """Validation happens BEFORE the first upload, so it must cover the whole set. Bailing
    on creative 1 leaves creatives 2 and 3 unchecked and a half-built campaign possible.
    """
    refs = (
        CreativeRef(ref="missing.txt", content_hash="0" * 64),
        CreativeRef(ref="b.txt", content_hash=sha(PAYLOAD_B)),
    )
    reads, errors = load_creatives(refs, str(creative_dir))
    assert [r.ref for r in reads] == ["missing.txt", "b.txt"]
    assert reads[1].ok is True
    assert len(errors) == 1


def test_load_creatives_of_an_empty_tuple_is_vacuously_clean():
    assert load_creatives((), "/abs") == ([], [])


def test_assert_all_ok_raises_when_any_creative_is_bad(creative_dir):
    refs = (
        CreativeRef(ref="a.txt", content_hash=sha(PAYLOAD_A)),
        CreativeRef(ref="b.txt", content_hash="0" * 64),
    )
    reads, _ = load_creatives(refs, str(creative_dir))
    with pytest.raises(CreativeHashMismatch, match="refusing to create anything"):
        assert_all_ok(reads)


def test_assert_all_ok_names_every_unmet_creative(creative_dir):
    refs = (
        CreativeRef(ref="a.txt", content_hash="0" * 64),
        CreativeRef(ref="b.txt", content_hash="1" * 64),
    )
    reads, _ = load_creatives(refs, str(creative_dir))
    with pytest.raises(CreativeHashMismatch) as exc:
        assert_all_ok(reads)
    assert "a.txt" in str(exc.value) and "b.txt" in str(exc.value)


def test_assert_all_ok_passes_when_everything_matches(creative_dir):
    refs = (
        CreativeRef(ref="a.txt", content_hash=sha(PAYLOAD_A)),
        CreativeRef(ref="b.txt", content_hash=sha(PAYLOAD_B)),
    )
    reads, _ = load_creatives(refs, str(creative_dir))
    assert assert_all_ok(reads) is None


def test_the_shipped_sample_creatives_match_the_hashes_in_the_shipped_config(monkeypatch, tmp_path):
    """The repository's own demo has to be self-consistent, from any working directory."""
    from conftest import CONFIG_DIR

    from ocm import config as cfgmod
    from ocm.paid.campaign import Campaign

    conf = cfgmod.load(CONFIG_DIR / "campaign.toml")
    campaign = Campaign.from_config(conf.data, source_dir=conf.source_dir)

    monkeypatch.chdir(tmp_path)
    reads, errors = load_creatives(campaign.creatives, campaign.source_dir)
    assert errors == []
    assert all(r.ok for r in reads)
