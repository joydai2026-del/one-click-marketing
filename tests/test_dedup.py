"""Near-duplicate rejection: the guard that stops the loop re-posting its greatest hits."""

from __future__ import annotations

import pytest

from ocm.evaluation.dedup import DedupIndex, shingles, similarity

# A body long enough that a 5-word shingle set is meaningful. Thirty words gives 26
# shingles, so a single changed word moves five of them and the arithmetic is checkable
# by hand rather than being whatever the function happened to return.
BASE = (
    "the review gate scores every draft against a written rubric before any human is asked "
    "to look at it because a human reviewing disqualified work is the most expensive thing "
    "in this whole pipeline today"
)
ONE_WORD_CHANGED = BASE.replace("expensive", "wasteful")
UNRELATED = (
    "attribution windows are pinned and stored on every row so that a setting changed in a "
    "web interface cannot silently redefine what the numbers already in the database mean "
    "for anybody reading them later"
)


def test_similarity_is_one_for_identical_text():
    assert similarity(BASE, BASE) == 1.0


def test_similarity_is_one_for_text_differing_only_cosmetically():
    """Dedup runs on the same normalized form the compliance floor uses."""
    assert similarity(BASE, f"  {BASE.upper()}\n\n ") == 1.0


def test_similarity_is_zero_for_disjoint_text():
    assert similarity("alpha bravo charlie delta echo", "one two three four five") == 0.0


@pytest.mark.parametrize("a,b", [(BASE, ONE_WORD_CHANGED), (BASE, UNRELATED), (BASE, "")])
def test_similarity_is_symmetric(a, b):
    """Jaccard is a set measure, so argument order must not change the verdict."""
    assert similarity(a, b) == similarity(b, a)


def test_similarity_is_bounded_to_the_unit_interval():
    for a, b in [(BASE, ONE_WORD_CHANGED), (BASE, UNRELATED), ("", "x")]:
        assert 0.0 <= similarity(a, b) <= 1.0


def test_a_one_word_edit_scores_high_and_unrelated_text_scores_low():
    """The whole reason a hash check is not a duplicate check."""
    near = similarity(BASE, ONE_WORD_CHANGED)
    far = similarity(BASE, UNRELATED)
    assert near > 0.6
    assert far < 0.1


def test_short_texts_shingle_to_the_whole_string():
    """Below k words there is no window to slide, so the text itself is the only shingle."""
    assert shingles("three short words", k=5) == {"three short words"}


# --------------------------------------------------------------------------------------
# the index
# --------------------------------------------------------------------------------------


@pytest.fixture
def index() -> DedupIndex:
    return DedupIndex(threshold=0.6, k=5)


def test_empty_corpus_returns_none(index):
    assert index.duplicate_of(BASE, "hash-1") is None
    assert len(index) == 0


def test_exact_hash_match_wins_even_when_the_text_is_completely_different(index):
    """Catches a retry or a double dispatch, where the body may have been regenerated.

    The hash guard and the similarity guard fail differently, so the hash guard must not
    depend on the text agreeing with anything.
    """
    index.add("prior-1", BASE, "hash-1")
    assert index.duplicate_of(UNRELATED, "hash-1") == ("exact-hash", 1.0)


def test_near_paraphrase_above_threshold_is_caught_and_names_the_match(index):
    index.add("prior-1", BASE, "hash-1")
    hit = index.duplicate_of(ONE_WORD_CHANGED, "hash-2")
    assert hit is not None
    ref, score = hit
    assert ref == "prior-1"
    assert score >= index.threshold


def test_unrelated_text_is_not_a_duplicate(index):
    index.add("prior-1", BASE, "hash-1")
    assert index.duplicate_of(UNRELATED, "hash-2") is None


def test_the_best_match_is_reported_not_merely_the_first(index):
    """An operator investigating a block needs the closest prior piece, not an arbitrary one."""
    index.add("far", UNRELATED, "hash-far")
    index.add("near", BASE, "hash-near")
    index.add("also-far", "completely different words appear in this particular sentence", "h3")
    ref, score = index.duplicate_of(ONE_WORD_CHANGED, "hash-new")
    assert ref == "near"
    assert score > 0.6


@pytest.mark.parametrize("threshold,expect_blocked", [(0.99, False), (0.6, True), (0.01, True)])
def test_threshold_is_the_only_knob_that_decides_a_near_duplicate(threshold, expect_blocked):
    idx = DedupIndex(threshold=threshold, k=5)
    idx.add("prior-1", BASE, "hash-1")
    assert (idx.duplicate_of(ONE_WORD_CHANGED, "hash-2") is not None) is expect_blocked


def test_from_config_reads_threshold_and_shingle_size():
    idx = DedupIndex.from_config({"threshold": 0.75, "shingle_k": 3})
    assert (idx.threshold, idx.k) == (0.75, 3)


def test_from_config_defaults_are_the_documented_ones():
    idx = DedupIndex.from_config({})
    assert (idx.threshold, idx.k) == (0.6, 5)


def test_len_counts_the_corpus_not_the_hashes():
    idx = DedupIndex()
    idx.add("a", BASE, "h1")
    idx.add("b", UNRELATED, "h2")
    assert len(idx) == 2
