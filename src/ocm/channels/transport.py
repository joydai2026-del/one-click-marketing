"""Transports. The one seam between this repository and the real internet.

`DryRunTransport` is the default everywhere. It records every request in order and returns
a deterministic synthetic response. Two properties make it useful rather than decorative:

    - it is DETERMINISTIC, so the end-to-end demo produces the same output every run and
      a test can assert on exact values;
    - it is INSPECTABLE, so a test can assert that a refused publish produced zero
      requests, which is a much stronger claim than asserting the function returned False.

`LiveTransport` is intentionally left as an unimplemented stub that raises. A portfolio
repository that shipped a half-written HTTP client for someone else's private API would be
worse than useless: it would be a maintenance liability that never gets exercised. The
shape shows where a real client plugs in.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .base import ChannelRequest, ChannelResponse


def _synthetic_id(request: ChannelRequest) -> str:
    """A stable fake external id derived from the request. Never looks like a real id."""
    basis = f"{request.channel}:{request.operation}:{sorted(request.payload.items())}"
    return "dryrun-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


@dataclass
class DryRunTransport:
    """Records requests, sends nothing, returns synthetic responses."""

    requests: list[ChannelRequest] = field(default_factory=list)
    # Deterministic synthetic engagement returned for collect operations, per channel.
    synthetic_metrics: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def is_dry_run(self) -> bool:
        return True

    def send(self, request: ChannelRequest) -> ChannelResponse:
        self.requests.append(request)
        if request.operation == "publish":
            ext = _synthetic_id(request)
            return ChannelResponse(
                ok=True,
                external_id=ext,
                external_url=f"https://example.invalid/{request.channel}/{ext}",
                dry_run=True,
            )
        if request.operation == "collect":
            metrics = self._metrics_for(request)
            return ChannelResponse(ok=True, data={"metrics": metrics}, dry_run=True)
        return ChannelResponse(
            ok=False, error=f"unknown operation {request.operation!r}", dry_run=True
        )

    def _metrics_for(self, request: ChannelRequest) -> dict[str, float]:
        configured = self.synthetic_metrics.get(request.channel)
        if configured is not None:
            return dict(configured)
        # Derive stable pseudo-metrics from the external id so different posts differ but
        # the same post always collects the same numbers.
        ext = str(request.payload.get("external_id", ""))
        seed = int(hashlib.sha256(ext.encode("utf-8")).hexdigest()[:8], 16)
        return {
            "impressions": float(500 + seed % 4500),
            "engagements": float(5 + seed % 180),
            "replies": float(seed % 12),
        }

    def requests_for(self, operation: str) -> list[ChannelRequest]:
        return [r for r in self.requests if r.operation == operation]


class LiveTransport:
    """Placeholder for a credentialed HTTP client. Deliberately not implemented here."""

    @property
    def is_dry_run(self) -> bool:
        return False

    def send(self, request: ChannelRequest) -> ChannelResponse:  # pragma: no cover
        raise NotImplementedError(
            "LiveTransport is a stub. This repository ships no network client and no "
            "credentials by design. Implement send() against your platform's API and "
            "inject it in place of DryRunTransport."
        )
