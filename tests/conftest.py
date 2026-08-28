"""Shared fixtures and small hand-built doubles.

Everything here is deterministic. No fixture reads the wall clock, opens a socket, or
depends on the process working directory, because a test that does any of those things
stops being evidence about the code and starts being evidence about the machine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ocm.channels.base import ValidationError
from ocm.evaluation.compliance import ComplianceFloor
from ocm.evaluation.dedup import DedupIndex
from ocm.evaluation.rubric import Dimension, Rubric
from ocm.generation.style import Axis, StyleSpace
from ocm.models import Draft, PublishRecord, new_id
from ocm.paid.campaign import Campaign, CreativeRef, Guardrails

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config" / "example"
SRC_DIR = REPO_ROOT / "src"

# A fixed instant well clear of any real clock, so nothing can accidentally pass because
# the test happened to run at the same moment as a default timestamp.
FIXED_NOW = 1_800_000_000.0


# --------------------------------------------------------------------------------------
# evaluation fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture
def permissive_compliance() -> ComplianceFloor:
    """A floor that objects to nothing, so a test can isolate the thing it is measuring."""
    return ComplianceFloor(forbidden_terms=[], max_links=None, min_chars=0, max_chars=None)


@pytest.fixture
def empty_dedup() -> DedupIndex:
    return DedupIndex(threshold=0.6, k=5)


@pytest.fixture
def flat_rubric() -> Rubric:
    """Two equally weighted dimensions and no floors: isolates threshold arithmetic."""
    return Rubric(
        version="test-flat",
        dimensions=[
            Dimension(name="alpha", description="a", weight=1.0),
            Dimension(name="beta", description="b", weight=1.0),
        ],
        threshold=3.5,
    )


def exploding_scorer(dimension, text):
    """A scorer that fails the test if it is ever called.

    Used to prove the gate's short circuit is real: if the rubric were scored after a hard
    floor tripped, this raises and the test fails loudly instead of the short circuit
    being asserted only through an output field that could be produced some other way.
    """
    raise AssertionError(
        f"the rubric was scored for dimension {dimension.name!r} even though a hard "
        f"floor had already tripped; the short circuit is not working"
    )


# --------------------------------------------------------------------------------------
# generation fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture
def small_space() -> StyleSpace:
    return StyleSpace(
        axes=(
            Axis(name="hook", values=("question", "number", "story")),
            Axis(name="format", values=("short", "list")),
        )
    )


def make_draft(
    body: str,
    *,
    channel: str = "substack",
    title: str = "",
    draft_id: str = "d1",
    derived_from: tuple[str, ...] = (),
) -> Draft:
    return Draft(
        draft_id=draft_id,
        channel=channel,
        title=title,
        body=body,
        derived_from=derived_from,
        created_at=FIXED_NOW,
    )


# --------------------------------------------------------------------------------------
# publishing doubles
# --------------------------------------------------------------------------------------


class RecordingAdapter:
    """A channel double that records the publisher's phase list AT CALL TIME.

    Capturing the phase list's contents inside `publish` is what makes the ordering claim
    testable: asserting the final list only shows that the phases were appended in some
    order eventually, not that `posting` was written BEFORE the outbound call.
    """

    def __init__(
        self,
        *,
        name: str = "fake",
        external_id: str = "ext-1",
        raise_timeout: bool = False,
        validation_errors: list[ValidationError] | None = None,
    ) -> None:
        self.name = name
        self.external_id = external_id
        self.raise_timeout = raise_timeout
        self.validation_errors = validation_errors or []
        self.phases_at_call: list[str] | None = None
        self.publish_calls = 0
        self.publisher: object | None = None

    def validate(self, draft: Draft) -> list[ValidationError]:
        return list(self.validation_errors)

    def publish(self, draft: Draft) -> PublishRecord:
        self.publish_calls += 1
        if self.publisher is not None:
            self.phases_at_call = list(self.publisher.phases)
        if self.raise_timeout:
            raise TimeoutError("the channel did not answer in time")
        return PublishRecord(
            publish_id=new_id("pub"),
            draft_id=draft.draft_id,
            channel=self.name,
            content_hash=draft.content_hash,
            external_id=self.external_id,
            external_url=f"https://example.invalid/{self.external_id}",
            dry_run=True,
            published_at=FIXED_NOW,
        )


# --------------------------------------------------------------------------------------
# paid fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture
def guardrails() -> Guardrails:
    return Guardrails(
        lifetime_budget_minor=25_000,
        currency="USD",
        decision_metric="cost_per_purchase",
    )


def make_campaign(
    *,
    guardrails: Guardrails,
    campaign_id: str = "camp-1",
    landing_url: str = "https://example.invalid/product",
    geo: tuple[str, ...] = ("US",),
    creatives: tuple[CreativeRef, ...] | None = None,
    source_dir: str = "/somewhere/absolute",
    run_days: int = 7,
) -> Campaign:
    return Campaign(
        campaign_id=campaign_id,
        objective="conversions",
        optimization_event="purchase",
        landing_url=landing_url,
        run_days=run_days,
        geo=geo,
        languages=("en",),
        creatives=creatives
        or (
            CreativeRef(ref="a.txt", content_hash="a" * 64),
            CreativeRef(ref="b.txt", content_hash="b" * 64),
        ),
        guardrails=guardrails,
        source_dir=source_dir,
    )
