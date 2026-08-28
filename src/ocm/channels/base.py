"""The one interface every organic channel implements.

WHY THIS SHAPE

Channels differ in every way that matters to a marketer and in almost no way that matters
to a pipeline. Substack wants a title and long body and reports opens; X wants a short body
and reports impressions. If those differences leak into the orchestrator, adding a channel
means editing the loop, and the loop becomes the place where every platform quirk
accumulates. So the orchestrator knows exactly four verbs:

    validate(draft)   can this channel carry this content at all
    publish(draft)    put it out, return proof
    collect(record)   pull engagement back
    normalize(...)    turn this channel's counters into one comparable score

`normalize` living on the ADAPTER rather than in the learning module is the load-bearing
decision. Only the channel knows that its "view" is cheap and its "reply" is expensive.
Ranking a Substack post against a tweet by raw engagement count compares nothing; each
adapter converts its own counters into a 0-1 score, and the ranker then works in one unit.

TRANSPORTS ARE INJECTED

An adapter never opens a socket. It builds a request and hands it to a `Transport`. The
default transport in this repository is `DryRunTransport`, which records the call and
returns a synthetic response. That is why the whole loop runs with no credentials: the
substitution point is one constructor argument, not a scattering of `if dry_run:` branches.
An `if dry_run:` inside a publish path is a bug waiting for the day someone inverts it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..models import Draft, EngagementRecord, PublishRecord


@dataclass(frozen=True)
class ChannelRequest:
    """A transport-agnostic description of one outbound call."""

    channel: str
    operation: str  # "publish" | "collect"
    payload: dict[str, Any]


@dataclass(frozen=True)
class ChannelResponse:
    ok: bool
    external_id: str = ""
    external_url: str = ""
    data: dict[str, Any] | None = None
    error: str = ""
    dry_run: bool = False


@runtime_checkable
class Transport(Protocol):
    """Performs a channel request. The only place a real network call could happen."""

    def send(self, request: ChannelRequest) -> ChannelResponse: ...

    @property
    def is_dry_run(self) -> bool: ...


@dataclass(frozen=True)
class ValidationError:
    field: str
    detail: str


class ChannelAdapter(Protocol):
    """The contract. Nothing outside `channels/` should know a channel's name."""

    name: str

    def validate(self, draft: Draft) -> list[ValidationError]:
        """Channel-specific structural limits: length caps, media counts, title rules."""
        ...

    def publish(self, draft: Draft) -> PublishRecord: ...

    def collect(self, record: PublishRecord) -> EngagementRecord: ...

    def normalize(self, engagement: EngagementRecord) -> float:
        """Map this channel's raw counters onto a comparable 0.0-1.0 score."""
        ...
