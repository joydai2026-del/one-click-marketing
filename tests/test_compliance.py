"""Hard compliance floors: evasion resistance, and the term-list leak guard."""

from __future__ import annotations

import pytest

from ocm.evaluation.compliance import ComplianceFloor, ComplianceViolation, normalize

TERM = "forbidden-widget"


def rules(v: list[ComplianceViolation]) -> set[str]:
    return {x.rule for x in v}


@pytest.fixture
def term_floor() -> ComplianceFloor:
    return ComplianceFloor(forbidden_terms=[TERM], min_chars=0)


@pytest.mark.parametrize(
    "body,why",
    [
        (f"we sell a {TERM} today", "plain substring"),
        (f"we sell a {TERM.upper()} today", "uppercase"),
        ("we sell a FoRbIdDeN-WiDgEt today", "mixed case"),
        # NFKC folds these lookalikes onto ordinary ASCII. Without the fold, a fullwidth
        # or ligature spelling is a different byte string and slips the list entirely.
        ("we sell a ｆｏｒｂｉｄｄｅｎ-widget today", "fullwidth latin"),
        ("we sell a ⓕorbidden-widget today", "circled latin letter"),
        ("we sell a 𝐟𝐨𝐫𝐛𝐢𝐝𝐝𝐞𝐧-widget today", "mathematical bold letters"),
        # Zero-width characters render as nothing but break naive matching.
        ("we sell a forbidden​-widget today", "zero width space"),
        ("we sell a for­bidden-widget today", "soft hyphen"),
        ("we sell a forbidden-wid﻿get today", "byte order mark"),
        # Whitespace collapsing is why doubled spaces and newlines do not help either.
        ("we sell a forbidden-widget\n\n today", "newlines around the term"),
    ],
)
def test_forbidden_term_survives_cosmetic_evasion(term_floor, body, why):
    """A term list that only matches the exact bytes is a term list that does not work."""
    assert "forbidden_term" in rules(term_floor.check(body)), f"evaded via {why}"


def test_forbidden_term_does_not_match_unrelated_text(term_floor):
    assert term_floor.check("an ordinary sentence about nothing in particular") == []


def test_whitespace_inside_the_term_is_collapsed_not_ignored(term_floor):
    """Collapsing runs of whitespace is not the same as deleting whitespace.

    "forbidden - widget" (spaces around the hyphen) is a DIFFERENT string, and the floor
    is documented as whitespace-normalizing, not whitespace-stripping. Pinning this stops
    a future "improvement" from silently widening the matcher into false positives.
    """
    assert term_floor.check("we sell a forbidden - widget") == []


def test_violation_detail_does_not_echo_the_term_by_default(term_floor):
    """The leak guard: the term list is the sensitive artifact, logs are the wide surface."""
    v = term_floor.check(f"we sell a {TERM}")
    assert len(v) == 1
    assert TERM not in v[0].detail
    assert TERM not in str(v[0])
    assert v[0].detail == "matched term #0"


def test_violation_detail_echoes_the_term_when_explicitly_enabled():
    floor = ComplianceFloor(forbidden_terms=[TERM], min_chars=0, echo_terms=True)
    v = floor.check(f"we sell a {TERM}")
    assert TERM in v[0].detail


def test_term_index_identifies_which_entry_matched_without_naming_it():
    """Reported by index so an operator can still find the rule in their own config."""
    floor = ComplianceFloor(forbidden_terms=["alpha", "bravo", "charlie"], min_chars=0)
    v = floor.check("we discussed charlie at length")
    assert [x.detail for x in v] == ["matched term #2"]


# --------------------------------------------------------------------------------------
# structural rules
# --------------------------------------------------------------------------------------


def test_max_links_zero_enforces_a_no_links_policy():
    floor = ComplianceFloor(max_links=0, min_chars=0)
    assert rules(floor.check("read more at https://example.invalid/x")) == {"max_links"}
    assert floor.check("read more in the archive") == []


@pytest.mark.parametrize("n_links,limit,should_trip", [(1, 1, False), (2, 1, True), (3, 5, False)])
def test_max_links_counts_urls_against_the_limit(n_links, limit, should_trip):
    body = " ".join(f"https://example.invalid/{i}" for i in range(n_links))
    floor = ComplianceFloor(max_links=limit, min_chars=0)
    assert ("max_links" in rules(floor.check(body))) is should_trip


def test_max_links_none_means_no_link_rule_at_all():
    floor = ComplianceFloor(max_links=None, min_chars=0)
    body = " ".join(f"https://example.invalid/{i}" for i in range(20))
    assert floor.check(body) == []


def test_min_chars_and_max_chars_bound_the_body():
    floor = ComplianceFloor(min_chars=10, max_chars=20)
    assert rules(floor.check("short")) == {"min_chars"}
    assert floor.check("just right ok") == []
    assert rules(floor.check("x" * 21)) == {"max_chars"}


def test_length_is_measured_on_the_stripped_body():
    """Surrounding whitespace must not buy a draft past a minimum length."""
    floor = ComplianceFloor(min_chars=10, max_chars=None)
    assert rules(floor.check("   tiny   ")) == {"min_chars"}


def test_required_markers_must_all_be_present():
    floor = ComplianceFloor(required_markers=["#ad", "paid partnership"], min_chars=0)
    v = floor.check("a post with #ad on it")
    assert [x.rule for x in v] == ["required_marker"]
    assert "paid partnership" in v[0].detail
    assert floor.check("#AD and Paid Partnership disclosed") == []


def test_required_marker_matching_is_normalized_like_the_term_list():
    """Otherwise a disclosure typed with a zero-width character would read as missing."""
    floor = ComplianceFloor(required_markers=["#ad"], min_chars=0)
    assert floor.check("disclosed: #​ad") == []


def test_all_independent_violations_are_reported_together():
    """One re-run per defect turns a three-field problem into three review cycles."""
    floor = ComplianceFloor(
        forbidden_terms=[TERM], max_links=0, min_chars=1000, required_markers=["#ad"]
    )
    assert rules(floor.check(f"{TERM} https://example.invalid/x")) == {
        "forbidden_term",
        "max_links",
        "min_chars",
        "required_marker",
    }


def test_from_config_round_trips_every_rule():
    floor = ComplianceFloor.from_config(
        {
            "forbidden_terms": ["a", "b"],
            "max_links": 0,
            "min_chars": 40,
            "max_chars": 280,
            "required_markers": ["#ad"],
            "echo_terms": True,
        }
    )
    assert floor.forbidden_terms == ["a", "b"]
    assert (floor.max_links, floor.min_chars, floor.max_chars) == (0, 40, 280)
    assert floor.required_markers == ["#ad"] and floor.echo_terms is True


def test_from_config_defaults_echo_terms_off():
    """The leak guard must be the default, not something an operator has to remember."""
    assert ComplianceFloor.from_config({}).echo_terms is False


def test_normalize_is_idempotent():
    once = normalize("  A​B   ｃ  ")
    assert normalize(once) == once


# --------------------------------------------------------------------------------------
# invisible-character evasion, handled categorically
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "codepoint,name",
    [
        (0x200B, "zero width space"),
        (0x200C, "zero width non-joiner"),
        (0x200D, "zero width joiner"),
        (0x200E, "left-to-right mark"),
        (0x200F, "right-to-left mark"),
        (0x202A, "left-to-right embedding"),
        (0x2060, "word joiner"),
        (0x2061, "function application"),
        (0xFEFF, "zero width no-break space"),
        (0x00AD, "soft hyphen"),
        (0x3164, "hangul filler"),
        (0x0001, "a control character"),
    ],
)
def test_an_invisible_character_cannot_split_a_forbidden_term(codepoint, name):
    """A HAND-WRITTEN LIST OF INVISIBLE CHARACTERS DOES NOT WORK.

    This started as a list of six well-known offenders and was evaded by U+200E, which was
    simply not on it. An attacker only has to find the one that was forgotten, so the rule
    is categorical: strip everything Unicode itself classifies as a format or control
    character. Each case here is a character that would evade a naive substring match.
    """
    floor = ComplianceFloor(forbidden_terms=["forbidden"])
    split = "forbid" + chr(codepoint) + "den"
    violations = floor.check(f"a sentence containing {split} in the middle")
    assert violations, f"{name} (U+{codepoint:04X}) evaded the forbidden-term check"
    assert violations[0].rule == "forbidden_term"


def test_a_visible_character_still_breaks_a_term_because_it_is_a_different_word():
    """The rule strips what renders as nothing, not what a reader can see. "forbid-den"
    with a real hyphen is a different string and must not be reported as the term."""
    floor = ComplianceFloor(forbidden_terms=["forbidden"])
    assert floor.check("a sentence containing forbid|den in the middle") == []
