"""Generation: the prompt frame, the avoid-block mask, and the fail-closed retry budget."""

from __future__ import annotations

import pytest

from ocm.evaluation.compliance import ComplianceFloor
from ocm.generation.generator import (
    FactBank,
    GenerationError,
    GenerationRequest,
    Generator,
    TemplateGenerator,
    build_prompt,
    mask_terms,
    normalized_equal,
)
from ocm.generation.style import Axis, Style, StyleSpace

TERM = "forbidden-widget"

BANK = FactBank(
    topics=("how the review gate works",),
    facts=(
        "the sample pipeline runs six stages per round",
        "the sample gate scores ten dimensions",
    ),
    voice="plain, specific, no hype",
)

STYLE = Style(coords=(("hook", "question"), ("format", "list")))


def request(**overrides) -> GenerationRequest:
    kwargs = dict(
        style=STYLE,
        topic="how the review gate works",
        channel="substack",
        bank=BANK,
        min_chars=200,
        max_chars=60_000,
    )
    kwargs.update(overrides)
    return GenerationRequest(**kwargs)


class ScriptedModel:
    """Returns queued completions in order and remembers every prompt it was handed."""

    def __init__(self, *completions: str) -> None:
        self.queue = list(completions)
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.queue.pop(0) if self.queue else "fallback completion"


# --------------------------------------------------------------------------------------
# build_prompt is pure
# --------------------------------------------------------------------------------------


def test_build_prompt_is_pure_and_repeatable():
    """It makes no model call, so it can be asserted on exactly rather than approximately."""
    req = request()
    assert build_prompt(req, [TERM]) == build_prompt(req, [TERM])


def test_build_prompt_states_the_topic_the_style_the_facts_and_the_length_band():
    prompt = build_prompt(request(), [])

    assert "Topic: how the review gate works" in prompt
    assert "- hook: question" in prompt
    assert "- format: list" in prompt
    for fact in BANK.facts:
        assert fact in prompt
    assert "Use ONLY these facts" in prompt
    assert "between 200 and 60000 characters" in prompt
    assert "substack" in prompt
    assert BANK.voice in prompt


def test_build_prompt_states_an_open_ended_band_when_there_is_no_maximum():
    """Stating the band is what stops long-form copy landing in a short-form field and
    then failing validation on every retry."""
    assert "at least 200 characters" in build_prompt(request(max_chars=None), [])


def test_build_prompt_lists_the_forbidden_terms_for_the_model():
    prompt = build_prompt(request(), [TERM])
    assert TERM in prompt
    assert "Never write any of the following" in prompt


def test_build_prompt_omits_optional_blocks_when_they_are_empty():
    prompt = build_prompt(request(), [])
    assert "Signals from the previous round" not in prompt
    assert "Do not repeat these recent pieces" not in prompt
    assert "The previous attempt was rejected" not in prompt


def test_build_prompt_includes_guidance_and_the_rejection_correction_when_present():
    prompt = build_prompt(
        request(guidance=("hook: 'question' scored highest",), correction="max_links: 2 links"),
        [],
    )
    assert "hook: 'question' scored highest" in prompt
    assert "The previous attempt was rejected: max_links: 2 links" in prompt


# --------------------------------------------------------------------------------------
# the masked avoid block
# --------------------------------------------------------------------------------------


def test_mask_terms_replaces_a_forbidden_term_case_insensitively():
    assert mask_terms(f"we sold a {TERM.upper()} today", [TERM]) == "we sold a [redacted] today"


def test_mask_terms_replaces_every_occurrence_and_preserves_the_surrounding_text():
    out = mask_terms(f"{TERM} then {TERM} again", [TERM])
    assert out == "[redacted] then [redacted] again"
    assert TERM not in out


def test_mask_terms_is_a_no_op_when_there_is_nothing_to_mask():
    assert mask_terms("clean text", [TERM]) == "clean text"
    assert mask_terms("clean text", []) == "clean text"
    assert mask_terms("clean text", [""]) == "clean text"


def test_the_avoid_block_is_masked_so_history_cannot_re_teach_a_banned_string():
    """The generator is shown recent posts so it does not repeat itself. If a forbidden
    term is in that history, showing it hands the model the exact string it was just told
    never to write.
    """
    prompt = build_prompt(request(avoid=(f"last week we mentioned {TERM} by name",)), [TERM])

    assert "Do not repeat these recent pieces:" in prompt
    avoid_block = prompt.split("Do not repeat these recent pieces:", 1)[1]
    assert TERM not in avoid_block
    assert "[redacted]" in avoid_block
    # The term still appears in the forbidden list above, which is where it belongs.
    assert TERM in prompt.split("Do not repeat these recent pieces:", 1)[0]


# --------------------------------------------------------------------------------------
# the retry budget
# --------------------------------------------------------------------------------------


def test_generator_retries_on_a_compliance_violation_with_the_reason_appended():
    compliance = ComplianceFloor(forbidden_terms=[TERM], min_chars=0)
    model = ScriptedModel(f"a draft mentioning {TERM}", "a clean second draft")
    gen = Generator(model=model, compliance=compliance, max_attempts=3)

    assert gen.generate(request()) == "a clean second draft"
    assert len(model.prompts) == 2
    assert "The previous attempt was rejected" not in model.prompts[0]
    assert "forbidden_term" in model.prompts[1]


def test_generator_retries_when_a_draft_is_too_similar_to_a_recent_piece():
    prior = (
        "the review gate scores every draft against a written rubric before any human is "
        "asked to look at it because attention is the expensive resource here"
    )
    model = ScriptedModel(prior, "a genuinely different second draft about other things")
    gen = Generator(
        model=model,
        compliance=ComplianceFloor(min_chars=0),
        max_attempts=3,
        avoid_similarity=0.6,
    )

    out = gen.generate(request(avoid=(prior,)))
    assert out == "a genuinely different second draft about other things"
    assert "too similar" in model.prompts[1]


def test_generator_raises_when_the_budget_is_exhausted_and_does_not_return_the_least_bad():
    """Fail closed. Returning the least-bad attempt publishes the worst output at exactly
    the moment the generator is malfunctioning.
    """
    compliance = ComplianceFloor(forbidden_terms=[TERM], min_chars=0)
    model = ScriptedModel(*[f"attempt {i} with {TERM}" for i in range(3)])
    gen = Generator(model=model, compliance=compliance, max_attempts=3)

    with pytest.raises(GenerationError) as exc:
        gen.generate(request())

    assert "exhausted 3 attempts" in str(exc.value)
    assert "forbidden_term" in str(exc.value)
    assert len(model.prompts) == 3


def test_the_exhaustion_error_does_not_echo_the_forbidden_term():
    """The rejection reason travels into logs, so the leak guard has to hold here too."""
    compliance = ComplianceFloor(forbidden_terms=[TERM], min_chars=0)
    gen = Generator(
        model=ScriptedModel(*[f"bad {TERM}" for _ in range(3)]),
        compliance=compliance,
        max_attempts=3,
    )
    with pytest.raises(GenerationError) as exc:
        gen.generate(request())
    assert TERM not in str(exc.value)


def test_a_clean_first_attempt_costs_exactly_one_model_call():
    model = ScriptedModel("a perfectly clean draft")
    gen = Generator(model=model, compliance=ComplianceFloor(min_chars=0), max_attempts=3)
    assert gen.generate(request()) == "a perfectly clean draft"
    assert len(model.prompts) == 1


def test_the_completion_is_stripped_before_it_is_checked():
    model = ScriptedModel("  padded draft  \n")
    gen = Generator(model=model, compliance=ComplianceFloor(min_chars=0))
    assert gen.generate(request()) == "padded draft"


def test_max_attempts_below_one_still_makes_exactly_one_attempt():
    """A misconfigured budget must not turn into an infinite loop or a zero-call no-op."""
    model = ScriptedModel("a clean draft")
    gen = Generator(model=model, compliance=ComplianceFloor(min_chars=0), max_attempts=0)
    assert gen.generate(request()) == "a clean draft"
    assert len(model.prompts) == 1


# --------------------------------------------------------------------------------------
# the fact bank
# --------------------------------------------------------------------------------------


def test_fact_bank_refuses_a_config_with_no_topics_or_no_facts():
    with pytest.raises(ValueError, match="no topics"):
        FactBank.from_config({"facts": ["a"]})
    with pytest.raises(ValueError, match="nothing could be grounded"):
        FactBank.from_config({"topics": ["a"]})


def test_fact_bank_reads_topics_facts_and_voice():
    bank = FactBank.from_config({"topics": ["t"], "facts": ["f"], "voice": "v"})
    assert (bank.topics, bank.facts, bank.voice) == (("t",), ("f",), "v")


# --------------------------------------------------------------------------------------
# the offline template generator
# --------------------------------------------------------------------------------------


def test_template_generator_is_deterministic():
    prompt = build_prompt(request(), [])
    assert TemplateGenerator().complete(prompt) == TemplateGenerator().complete(prompt)


@pytest.mark.parametrize("max_chars", [280, 500, 1000])
def test_template_generator_honors_the_short_form_maximum(max_chars):
    """A long-form draft pasted into a short-form field fails channel validation forever."""
    text = TemplateGenerator().complete(
        build_prompt(request(channel="x", min_chars=0, max_chars=max_chars), [])
    )
    assert len(text) <= max_chars


@pytest.mark.parametrize("min_chars", [200, 800, 2000])
def test_template_generator_honors_the_long_form_minimum(min_chars):
    text = TemplateGenerator().complete(
        build_prompt(request(min_chars=min_chars, max_chars=60_000), [])
    )
    assert len(text) >= min_chars


def test_template_generator_output_is_visibly_fabricated():
    """It is not a writing model and the repository must never let it look like one."""
    text = TemplateGenerator().complete(build_prompt(request(), []))
    assert "SAMPLE CONTENT" in text
    assert "Not marketing copy." in text


def test_template_generator_cites_only_facts_from_the_prompt():
    """Grounding, checked on the one generator this repository actually ships."""
    text = TemplateGenerator().complete(build_prompt(request(), []))
    for fact in BANK.facts:
        assert fact in text


def test_template_generator_varies_output_by_topic_so_drafts_are_not_near_duplicates():
    gen = TemplateGenerator()
    a = gen.complete(build_prompt(request(topic="topic one"), []))
    b = gen.complete(build_prompt(request(topic="topic two"), []))
    assert a != b


def test_template_generator_records_every_prompt_it_was_given():
    gen = TemplateGenerator()
    gen.complete(build_prompt(request(), []))
    gen.complete(build_prompt(request(topic="another"), []))
    assert len(gen._calls) == 2


def test_the_template_generator_round_trips_through_a_real_style_space():
    """Guards the prompt-parsing helpers against a change in build_prompt's layout."""
    space = StyleSpace(
        axes=(Axis("hook", ("question", "story")), Axis("format", ("list", "walkthrough")))
    )
    style = space.rotation_at(1, 1)[0]
    text = TemplateGenerator().complete(build_prompt(request(style=style), []))
    for _, value in style.coords:
        assert value in text


# --------------------------------------------------------------------------------------
# cross-channel identical check
# --------------------------------------------------------------------------------------


def test_normalized_equal_ignores_only_cosmetic_differences():
    assert normalized_equal("Hello   World", "hello world") is True
    assert normalized_equal("hello world", "hello  worlds") is False
