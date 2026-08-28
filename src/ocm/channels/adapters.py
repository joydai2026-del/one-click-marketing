"""Two example channel adapters.

They are examples in the strict sense: they demonstrate the interface and the per-channel
normalization idea against two platforms with genuinely different economics. Neither talks
to a real API here. Both are configured entirely from `config/example/channels.toml`.

The interesting difference is `normalize`:

    A long-form newsletter is read by people who already subscribed. Absolute engagement
    is the signal, because reach is roughly fixed and a post that got twice the replies
    really was twice as good.

    A short-form public feed shows a post to a variable and largely unpredictable number
    of people. Absolute engagement mostly measures how the ranking system felt that hour,
    so the signal is the RATE: engagements divided by impressions.

Ranking those two on one leaderboard by raw counts would let a single viral short post
drown out every newsletter forever, and the loop would learn the wrong lesson. Each adapter
therefore emits a 0-1 score in its own terms and the ranker never sees a raw counter.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Draft, EngagementRecord, PublishRecord, new_id, now_ts
from .base import ChannelRequest, ValidationError
from .transport import DryRunTransport


@dataclass
class LongFormAdapter:
    """Newsletter-style channel: title required, long body, absolute engagement scoring.

    Configured as `substack` in the example config to make the mapping concrete.
    """

    name: str = "substack"
    transport: object = field(default_factory=DryRunTransport)
    max_body_chars: int = 60_000
    min_body_chars: int = 200
    require_title: bool = True
    # Engagement count that counts as a top-scoring post. Config, because it is a property
    # of the list size, not of the code.
    engagement_saturation: float = 100.0

    @classmethod
    def from_config(cls, cfg: dict, transport: object) -> LongFormAdapter:
        return cls(
            name=cfg.get("name", "substack"),
            transport=transport,
            max_body_chars=int(cfg.get("max_body_chars", 60_000)),
            min_body_chars=int(cfg.get("min_body_chars", 200)),
            require_title=bool(cfg.get("require_title", True)),
            engagement_saturation=float(cfg.get("engagement_saturation", 100.0)),
        )

    def validate(self, draft: Draft) -> list[ValidationError]:
        errs: list[ValidationError] = []
        if self.require_title and not draft.title.strip():
            errs.append(ValidationError("title", "this channel requires a title"))
        n = len(draft.body.strip())
        if n < self.min_body_chars:
            errs.append(ValidationError("body", f"{n} chars, minimum {self.min_body_chars}"))
        if n > self.max_body_chars:
            errs.append(ValidationError("body", f"{n} chars, maximum {self.max_body_chars}"))
        return errs

    def publish(self, draft: Draft) -> PublishRecord:
        resp = self.transport.send(
            ChannelRequest(
                channel=self.name,
                operation="publish",
                payload={"title": draft.title, "body": draft.body},
            )
        )
        if not resp.ok:
            raise RuntimeError(f"{self.name} publish failed: {resp.error}")
        return PublishRecord(
            publish_id=new_id("pub"),
            draft_id=draft.draft_id,
            channel=self.name,
            content_hash=draft.content_hash,
            external_id=resp.external_id,
            external_url=resp.external_url,
            dry_run=resp.dry_run,
        )

    def collect(self, record: PublishRecord) -> EngagementRecord:
        resp = self.transport.send(
            ChannelRequest(
                channel=self.name,
                operation="collect",
                payload={"external_id": record.external_id},
            )
        )
        metrics = dict((resp.data or {}).get("metrics", {}))
        return EngagementRecord(
            publish_id=record.publish_id,
            channel=self.name,
            collected_at=now_ts(),
            metrics=metrics,
        )

    def normalize(self, engagement: EngagementRecord) -> float:
        """Absolute engagement against a configured saturation point."""
        eng = float(engagement.metrics.get("engagements", 0.0))
        if self.engagement_saturation <= 0:
            return 0.0
        return max(0.0, min(1.0, eng / self.engagement_saturation))


@dataclass
class ShortFormAdapter:
    """Public-feed channel: hard length cap, no title, engagement-RATE scoring.

    Configured as `x` in the example config.
    """

    name: str = "x"
    transport: object = field(default_factory=DryRunTransport)
    max_body_chars: int = 280
    min_impressions_for_signal: int = 100
    rate_saturation: float = 0.05

    @classmethod
    def from_config(cls, cfg: dict, transport: object) -> ShortFormAdapter:
        return cls(
            name=cfg.get("name", "x"),
            transport=transport,
            max_body_chars=int(cfg.get("max_body_chars", 280)),
            min_impressions_for_signal=int(cfg.get("min_impressions_for_signal", 100)),
            rate_saturation=float(cfg.get("rate_saturation", 0.05)),
        )

    def validate(self, draft: Draft) -> list[ValidationError]:
        errs: list[ValidationError] = []
        n = len(draft.body.strip())
        if n == 0:
            errs.append(ValidationError("body", "empty body"))
        if n > self.max_body_chars:
            errs.append(ValidationError("body", f"{n} chars, maximum {self.max_body_chars}"))
        if draft.title.strip():
            errs.append(ValidationError("title", "this channel does not carry a title"))
        return errs

    def publish(self, draft: Draft) -> PublishRecord:
        resp = self.transport.send(
            ChannelRequest(
                channel=self.name, operation="publish", payload={"body": draft.body}
            )
        )
        if not resp.ok:
            raise RuntimeError(f"{self.name} publish failed: {resp.error}")
        return PublishRecord(
            publish_id=new_id("pub"),
            draft_id=draft.draft_id,
            channel=self.name,
            content_hash=draft.content_hash,
            external_id=resp.external_id,
            external_url=resp.external_url,
            dry_run=resp.dry_run,
        )

    def collect(self, record: PublishRecord) -> EngagementRecord:
        resp = self.transport.send(
            ChannelRequest(
                channel=self.name,
                operation="collect",
                payload={"external_id": record.external_id},
            )
        )
        metrics = dict((resp.data or {}).get("metrics", {}))
        return EngagementRecord(
            publish_id=record.publish_id,
            channel=self.name,
            collected_at=now_ts(),
            metrics=metrics,
        )

    def normalize(self, engagement: EngagementRecord) -> float:
        """Engagement rate, with a floor on impressions.

        Below the impression floor the rate is statistical noise: three engagements on
        eleven impressions is a 27 percent rate and means nothing. Returning 0.0 rather
        than a flattering ratio keeps under-delivered posts from being learned from.
        """
        impressions = float(engagement.metrics.get("impressions", 0.0))
        eng = float(engagement.metrics.get("engagements", 0.0))
        if impressions < self.min_impressions_for_signal:
            return 0.0
        rate = eng / impressions if impressions else 0.0
        if self.rate_saturation <= 0:
            return 0.0
        return max(0.0, min(1.0, rate / self.rate_saturation))
