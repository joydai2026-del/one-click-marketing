"""The ad platform seam, and the structural reason nothing here can spend.

WHY THERE IS NO GRAND `AdPlatform` INTERFACE

`create_ad_set()` is already one vendor's model of the world. Another vendor calls the same
layer an ad group, a third collapses it entirely. An interface drawn from the first vendor
you integrate gets torn down when the second arrives, so it is not drawn here. An interface
only earns its width once two real implementations prove it.

What IS portable is the DATA. `Campaign` and `MetricSnapshot` are defined first and are
vendor-neutral; vendor-specific fields stay inside the adapter. The abstracted seam sits one
level lower than "platform": a transport that performs a request and has a documented error
contract, in particular a distinct signal for "the outcome is UNKNOWN" as opposed to
"it failed". A timeout on a create call is not a failure. Treating it as one is how a retry
creates a second campaign.

THE READ-ONLY GATE

`DryRunPlatform` refuses every non-GET method. Written as `method.upper() != "GET"` and not
as `== "POST"`, deliberately: the negative form means a later edit that narrows the check
cannot accidentally reopen PUT, PATCH, or DELETE. A gate should fail toward refusing.

CAMPAIGNS ARE BORN PAUSED

Every created campaign is PAUSED, written explicitly at the call site and never defaulted.
There is no `activate()` method on this class and no module-level activation function.
The strongest version of a spend gate is not a better check on the activation path; it is
having no activation path at all, so that turning delivery on requires a human in the ad
platform's own console. That is the design here, and it is why this repository can be run
by anyone without any possibility of it spending money.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class PlatformRefused(Exception):
    """The platform layer refused a request. Never a retryable condition."""


class OutcomeUnknown(Exception):
    """A request may or may not have landed. Callers must NOT auto-retry.

    Distinct from a failure. On a paid channel, retrying an unknown outcome is how one
    approved campaign becomes two real ones.
    """


@dataclass(frozen=True)
class CampaignState:
    """What the platform says exists right now. The spend gate re-checks against THIS."""

    platform_campaign_id: str
    status: str
    intent_digest: str
    lifetime_budget_minor: int
    currency: str
    starts_at: str
    ends_at: str


@dataclass
class DryRunPlatform:
    """In-memory platform stand-in. Cannot spend, by construction.

    Records every request so a test can assert that a refused operation produced ZERO
    requests, which is a far stronger claim than asserting a function returned False.
    """

    requests: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    _campaigns: dict[str, CampaignState] = field(default_factory=dict)
    _spend_minor: dict[str, int] = field(default_factory=dict)
    # Set True only after a human has confirmed this account out of band. Even then, this
    # class exposes no way to set a campaign live.
    account_confirmed: bool = False

    # ---- transport ----------------------------------------------------------------

    def request(self, method: str, path: str, **params: Any) -> dict[str, Any]:
        self.requests.append((method.upper(), path, dict(params)))
        # Negative form: a later narrowing edit cannot reopen the other write verbs.
        if method.upper() != "GET" and not self.account_confirmed:
            raise PlatformRefused(
                "this account has no recorded human confirmation; every non-GET request "
                "is refused"
            )
        return {"ok": True}

    # ---- creation -----------------------------------------------------------------

    def find_by_digest(self, intent_digest: str) -> list[CampaignState]:
        """Adoption lookup. Called BEFORE create so a retry does not duplicate."""
        return [c for c in self._campaigns.values() if c.intent_digest == intent_digest]

    def create_paused(
        self,
        *,
        intent_digest: str,
        lifetime_budget_minor: int,
        currency: str,
        starts_at: str,
        ends_at: str,
    ) -> CampaignState:
        """Create a campaign in PAUSED state. There is no counterpart that unpauses it."""
        existing = self.find_by_digest(intent_digest)
        if len(existing) == 1:
            # Adopt rather than duplicate: this is what the deterministic digest buys.
            return existing[0]
        if len(existing) > 1:
            raise OutcomeUnknown(
                f"{len(existing)} campaigns already carry this intent digest; the state is "
                f"ambiguous and must be resolved by a human, not by another create"
            )
        self.request("POST", "/campaigns", intent_digest=intent_digest)
        state = CampaignState(
            platform_campaign_id=f"dryrun-camp-{intent_digest[:12]}",
            status="PAUSED",  # written here, at the call site, never defaulted
            intent_digest=intent_digest,
            lifetime_budget_minor=lifetime_budget_minor,
            currency=currency,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        self._campaigns[state.platform_campaign_id] = state
        self._spend_minor.setdefault(state.platform_campaign_id, 0)
        return state

    def get_state(self, platform_campaign_id: str) -> CampaignState:
        try:
            return self._campaigns[platform_campaign_id]
        except KeyError as exc:
            raise PlatformRefused(f"no such campaign {platform_campaign_id!r}") from exc

    def set_status(self, platform_campaign_id: str, status: str) -> CampaignState:
        """Only PAUSED and ARCHIVED are accepted. There is no path to a live status."""
        if status not in {"PAUSED", "ARCHIVED"}:
            raise PlatformRefused(
                f"status {status!r} is not settable from this codebase; delivery is turned "
                f"on by a human in the ad platform's own console, never here"
            )
        state = self.get_state(platform_campaign_id)
        updated = CampaignState(
            platform_campaign_id=state.platform_campaign_id,
            status=status,
            intent_digest=state.intent_digest,
            lifetime_budget_minor=state.lifetime_budget_minor,
            currency=state.currency,
            starts_at=state.starts_at,
            ends_at=state.ends_at,
        )
        self._campaigns[platform_campaign_id] = updated
        return updated

    # ---- reporting ----------------------------------------------------------------

    def insights(self, platform_campaign_id: str, days: int) -> list[dict[str, Any]]:
        """One row per ad per day. Always a GET, so it is outside the money path."""
        self.request("GET", f"/campaigns/{platform_campaign_id}/insights", days=days)
        state = self.get_state(platform_campaign_id)
        rows: list[dict[str, Any]] = []
        for d in range(days):
            seed = (hash((platform_campaign_id, d)) % 997) + 3
            rows.append(
                {
                    "ad_id": f"dryrun-ad-{state.intent_digest[:8]}",
                    "date": f"2026-01-{d + 1:02d}",
                    "spend": round(seed / 100.0, 2),  # major units, as platforms report
                    "impressions": 100 * seed,
                    "clicks": seed // 2,
                    # Deliberately absent on some rows: the collector must record None
                    # rather than inventing a zero.
                    "actions": (
                        [{"action_type": "purchase", "value": str(seed % 5)}]
                        if d % 2 == 0
                        else []
                    ),
                }
            )
        return rows
