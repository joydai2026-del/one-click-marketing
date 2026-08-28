"""The orchestrator: one full round of the loop, organic and paid.

    generate -> gate -> approve -> publish -> collect -> learn -> (next round)

Read this file to see how the pieces compose. Everything it calls is tested in isolation;
this module's job is ordering and nothing else.

TWO ORDERING DECISIONS CARRY REAL WEIGHT

THE SCHEDULE CHECK COMES BEFORE GENERATION. Checking "may this channel publish right now?"
before calling the generator means a capped channel costs zero model calls. Checking after
means paying for content that is thrown away, every tick, forever.

THE GATE COMES BEFORE THE APPROVAL REQUEST. A human should never be asked to review
something the machine already knows is disqualified. The gate's job is to make the human's
attention expensive to waste.

WHAT "HUMAN APPROVES BEFORE ANYTHING PUBLISHES OR SPENDS" MEANS HERE

Two gate modes, both config-driven, and the difference between them is precise:

    human   the run stops. A signed approval must be supplied out of band.
    auto    the run may proceed WITHOUT the human tap, but ONLY for a draft that passed
            every check with zero flags. Auto skips the TAP, never the CHECK.

A draft that fails in auto mode is left PENDING for a human. It is never auto-rejected:
the machine is trusted to say "this is clean", not to say "this is bad".

Both graduation latches ship OFF. `auto` mode requires the config to opt in AND a separate
`auto_approval_enabled` flag to be set, so a single edit cannot silently make the loop
unattended.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .approval.ledger import InMemoryLedger
from .approval.tokens import ephemeral_key, issue, verify_and_consume
from .evaluation.gate import QualityGate
from .generation.generator import (
    FactBank,
    GenerationError,
    GenerationRequest,
    Generator,
    normalized_equal,
)
from .generation.identity import slot_id, variant_id
from .generation.style import StyleSpace
from .learning.ranker import Learnings, Ranker, Sample, Tilt
from .models import Draft, EngagementRecord, EvalResult, PublishRecord, Stage
from .publishing import GateError, PostLog, Publisher, SchedulePolicy, transition


@dataclass
class RoundResult:
    round_index: int
    drafted: list[Draft] = field(default_factory=list)
    evaluations: list[EvalResult] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    published: list[PublishRecord] = field(default_factory=list)
    engagement: dict[str, EngagementRecord] = field(default_factory=dict)
    learnings: dict[str, dict[str, Learnings]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class OrganicLoop:
    """One channel-spanning round of the organic loop."""

    space: StyleSpace
    bank: FactBank
    generator: Generator
    gate: QualityGate
    channels: dict[str, Any]
    ranker: Ranker
    tilt: Tilt
    schedule: SchedulePolicy
    log: PostLog
    gate_mode: str = "human"
    auto_approval_enabled: bool = False  # second latch; ships OFF
    approval_key: bytes = field(default_factory=ephemeral_key)
    ledger: InMemoryLedger = field(default_factory=InMemoryLedger)
    _texts: dict[str, str] = field(default_factory=dict)
    _samples: list[Sample] = field(default_factory=list)

    def run_round(
        self,
        round_index: int,
        *,
        learnings: dict[str, dict[str, Learnings]] | None = None,
        now: float | None = None,
    ) -> RoundResult:
        now = now if now is not None else time.time()
        learnings = learnings or {}
        result = RoundResult(round_index=round_index)

        staged: list[tuple[str, Draft]] = []

        for channel_name, adapter in self.channels.items():
            # 1. Schedule first: a capped channel costs zero model calls.
            allowed, reason = self.schedule.may_publish(self.log, channel_name, now=now)
            if not allowed:
                result.skipped.append(f"{channel_name}: {reason}")
                continue

            # 2. Pick a style. The durable confirmed-post count is the rotation offset, so
            #    a skipped tick reuses its slot instead of burning it.
            position = self.log.total(channel_name) + round_index
            style, exploring = self.tilt.style_for_round(
                self.space, position, learnings.get(channel_name, {})
            )
            topic = self.bank.topics[position % len(self.bank.topics)]
            if exploring:
                result.notes.append(
                    f"{channel_name}: position {position} is an exploration slot; "
                    f"winners ignored so the style space stays covered"
                )

            signal_guidance: tuple[str, ...] = ()
            chan_learn = learnings.get(channel_name, {})
            if chan_learn:
                sig = self.ranker.signal(
                    channel=channel_name,
                    round_index=round_index,
                    samples=self._samples,
                    learnings=chan_learn,
                )
                signal_guidance = sig.guidance

            # 3. Generate, grounded, with a bounded retry budget. The channel's own length
            #    band is passed in so the generator never needs to know which channels
            #    exist, and so a short-form field never receives long-form copy.
            needs_title = bool(getattr(adapter, "require_title", False))
            try:
                text = self.generator.generate(
                    GenerationRequest(
                        style=style,
                        topic=topic,
                        channel=channel_name,
                        bank=self.bank,
                        avoid=tuple(self._texts.values())[-6:],
                        guidance=signal_guidance,
                        min_chars=int(getattr(adapter, "min_body_chars", 0)),
                        max_chars=getattr(adapter, "max_body_chars", None),
                        needs_title=needs_title,
                    )
                )
            except GenerationError as exc:
                # One slot fails; the round continues. A generator problem on one channel
                # must not silently take down the others.
                result.skipped.append(f"{channel_name}: generation failed ({exc})")
                continue

            title = topic if needs_title else ""
            draft = Draft(
                draft_id=variant_id(
                    f"r{round_index}", channel_name, slot_id(style.style_id, topic)
                ),
                channel=channel_name,
                title=title,
                body=text,
                derived_from=tuple(f"{k}={v}" for k, v in style.tags().items()),
            )
            staged.append((channel_name, draft))
            result.drafted.append(draft)

        # 4. Cross-channel identical check. HELD for a human rather than regenerated:
        #    identical copy on two surfaces is a judgment call (sometimes it is exactly
        #    what you want), not a defect the generator should be told to fix.
        held_ids: set[str] = set()
        for i, (_, a) in enumerate(staged):
            for _, b in staged[i + 1 :]:
                if normalized_equal(a.body, b.body):
                    result.held.append(
                        f"{a.draft_id} and {b.draft_id} are identical across channels"
                    )
                    held_ids.add(a.draft_id)
                    held_ids.add(b.draft_id)

        for channel_name, draft in staged:
            stage = Stage.DRAFTED

            # 5. The quality gate.
            ev = self.gate.evaluate(draft)
            result.evaluations.append(ev)
            stage = transition(stage, Stage.EVALUATED if ev.passed else Stage.REJECTED)
            if not ev.passed:
                result.blocked.append(f"{draft.draft_id}: {'; '.join(ev.hard_failures) or 'below threshold'}")
                continue
            if draft.draft_id in held_ids:
                result.skipped.append(f"{draft.draft_id}: held for a human")
                continue

            # 6. Approval. This is where the loop stops in human mode.
            approved = self._approve(draft, ev)
            if not approved:
                result.skipped.append(
                    f"{draft.draft_id}: awaiting human approval "
                    f"(gate_mode={self.gate_mode}, auto_enabled={self.auto_approval_enabled})"
                )
                continue
            stage = transition(stage, Stage.APPROVED)

            # 7. Publish.
            adapter = self.channels[channel_name]
            publisher = Publisher(log=self.log)
            try:
                # The publisher stamps published_at from THIS clock, so the durable log and
                # the schedule cap are always read against the same clock they were
                # written with. See Publisher.publish.
                record = publisher.publish(adapter, draft, now=now)
            except GateError as exc:
                result.skipped.append(f"{draft.draft_id}: {exc}")
                continue
            stage = transition(stage, Stage.PUBLISHED)
            result.published.append(record)
            self._texts[draft.draft_id] = draft.body
            self.gate.dedup.add(draft.draft_id, draft.body, draft.content_hash)

            # 8. Collect.
            eng = adapter.collect(record)
            result.engagement[record.publish_id] = eng
            transition(stage, Stage.COLLECTED)
            self._samples.append(
                Sample(
                    variant_id=draft.draft_id,
                    channel=channel_name,
                    tags=dict(p.split("=", 1) for p in draft.derived_from),
                    score=adapter.normalize(eng),
                    excerpt=draft.body,
                )
            )

        # 9. Learn.
        for channel_name in self.channels:
            result.learnings[channel_name] = self.ranker.rank_all(
                self._samples, channel=channel_name, dimensions=self.space.dimensions
            )
        return result

    def _approve(self, draft: Draft, ev: EvalResult) -> bool:
        """Human mode stops. Auto mode proceeds only for a completely clean draft."""
        if self.gate_mode != "auto":
            return False
        if not self.auto_approval_enabled:
            # Second latch. Both must be on; one edit is not enough to go unattended.
            return False
        if ev.hard_failures or ev.notes:
            # Auto skips the TAP, never the CHECK. Anything flagged waits for a human and
            # is never auto-rejected.
            return False

        token, sig = issue(
            scope=f"publish:{draft.channel}",
            content_hash=draft.content_hash,
            subject=draft.draft_id,
            key=self.approval_key,
            approver="auto-gate",
        )
        try:
            verify_and_consume(
                token=token,
                signature=sig,
                key=self.approval_key,
                expected_scope=f"publish:{draft.channel}",
                # Recomputed from the draft about to be sent, not read off the token.
                expected_content_hash=draft.content_hash,
                ledger=self.ledger,
            )
        except Exception:
            return False
        return True
