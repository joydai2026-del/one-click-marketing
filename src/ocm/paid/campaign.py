"""Paid campaign configuration and spend guardrails.

Everything a campaign IS lives in config. Nothing about a campaign's budget, geography,
objective, or stopping rule is expressible only in code, because the moment it is, changing
a budget means a deploy and a deploy at 2am is how budgets get changed wrong.

THREE CHOICES WORTH EXPLAINING

LIFETIME BUDGET, NOT DAILY. Ad platforms treat a daily budget as a target they may
overshoot, sometimes substantially, and they make it up later. A daily figure is therefore
not a cap and must not be presented as one. A lifetime budget is a real ceiling, so that is
what the approval binds and what the guardrail checks.

MONEY IS INTEGER MINOR UNITS. Budgets are integers in the currency's minor unit (cents),
never floats. Float arithmetic on money produces a cap that is occasionally a hundredth
over, and "occasionally over the approved ceiling" is the one thing a spend guard may not
be.

THE DECISION METRIC IS PRE-REGISTERED. `decision_metric` is required and must be non-empty.
The number a campaign will be judged on is named BEFORE any money moves. Choosing it
afterwards, from the results, is how every campaign becomes a success: there is always some
metric that went up.

UNKNOWN KEYS ARE AN ERROR. A typo'd config key that is silently ignored gives you a
campaign running with a default you never chose. Config parsing here uses a closed
allowlist and raises on anything else.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_GUARDRAIL_KEYS = {
    "lifetime_budget_minor",
    "currency",
    "max_active_campaigns",
    "account_ceiling_minor",
    "decision_metric",
    "max_collection_failures",
}

_CAMPAIGN_KEYS = {
    "campaign_id",
    "objective",
    "optimization_event",
    "landing_url",
    "run_days",
    "geo",
    "languages",
    "creatives",
    "guardrails",
}


def _reject_unknown(cfg: dict, allowed: set[str], what: str) -> None:
    unknown = set(cfg) - allowed
    if unknown:
        raise ValueError(
            f"unknown {what} key(s): {sorted(unknown)}. "
            f"Keys are an allowlist so a typo cannot silently select a default."
        )


@dataclass(frozen=True)
class Guardrails:
    """What may be spent, and what ends the round. All required, none defaulted to money."""

    lifetime_budget_minor: int
    currency: str
    decision_metric: str
    max_active_campaigns: int = 1
    account_ceiling_minor: int | None = None
    max_collection_failures: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.lifetime_budget_minor, int):
            raise ValueError("lifetime_budget_minor must be an integer in minor units")
        if self.lifetime_budget_minor <= 0:
            raise ValueError("lifetime_budget_minor must be positive")
        if not (len(self.currency) == 3 and self.currency.isalpha() and self.currency.isupper()):
            raise ValueError("currency must be an uppercase ISO-4217 alpha-3 code")
        if not self.decision_metric.strip():
            raise ValueError(
                "decision_metric is required: name the number this round is judged on "
                "before any money moves"
            )
        if self.max_active_campaigns < 0:
            raise ValueError("max_active_campaigns must be non-negative")
        if self.max_active_campaigns > 1 and self.account_ceiling_minor is None:
            # N campaigns each at the per-campaign cap is N times the exposure. Without an
            # account ceiling the "budget" is per-campaign only, which reads as a total.
            raise ValueError(
                "account_ceiling_minor is required once max_active_campaigns exceeds 1"
            )

    @classmethod
    def from_config(cls, cfg: dict) -> Guardrails:
        _reject_unknown(cfg, _GUARDRAIL_KEYS, "guardrail")
        return cls(
            lifetime_budget_minor=cfg["lifetime_budget_minor"],
            currency=str(cfg["currency"]),
            decision_metric=str(cfg["decision_metric"]),
            max_active_campaigns=int(cfg.get("max_active_campaigns", 1)),
            account_ceiling_minor=cfg.get("account_ceiling_minor"),
            max_collection_failures=int(cfg.get("max_collection_failures", 3)),
        )


@dataclass(frozen=True)
class CreativeRef:
    """A creative identified by WHAT IT IS, not by where a platform happened to put it.

    Both fields are required. A ref with no hash is a pointer to whatever is at that path
    right now, which is exactly the binding this system exists to prevent.
    """

    ref: str
    content_hash: str

    def __post_init__(self) -> None:
        if not self.ref:
            raise ValueError("creative ref must not be empty")
        if not self.content_hash:
            raise ValueError(
                "creative content_hash is required: a ref without a hash binds to "
                "whatever is at that path at read time"
            )


@dataclass(frozen=True)
class Campaign:
    """What is bought. Deliberately carries no start date.

    The concrete start instant is decided once, at review-card time, so it appears on the
    document the human actually reads rather than being computed later by whichever
    process happened to run.
    """

    campaign_id: str
    objective: str
    optimization_event: str
    landing_url: str
    run_days: int
    geo: tuple[str, ...]
    languages: tuple[str, ...]
    creatives: tuple[CreativeRef, ...]
    guardrails: Guardrails
    # Absolute directory the config was loaded from. Relative creative refs resolve
    # against THIS, never against the process working directory.
    source_dir: str = ""

    def __post_init__(self) -> None:
        if not self.campaign_id.strip():
            raise ValueError("campaign_id is required")
        if self.run_days <= 0:
            raise ValueError("run_days must be positive")
        if not self.creatives:
            raise ValueError("a campaign with no creatives cannot be approved")
        for g in self.geo:
            if not (len(g) == 2 and g.isalpha() and g.isupper()):
                raise ValueError(f"geo {g!r} must be an uppercase ISO-3166-1 alpha-2 code")
        if len({c.ref for c in self.creatives}) != len(self.creatives):
            raise ValueError("duplicate creative refs")

    @classmethod
    def from_config(cls, cfg: dict, *, source_dir: str = "") -> Campaign:
        _reject_unknown(cfg, _CAMPAIGN_KEYS, "campaign")
        creatives = tuple(
            CreativeRef(ref=c["ref"], content_hash=c["content_hash"])
            for c in cfg.get("creatives", [])
        )
        return cls(
            campaign_id=str(cfg["campaign_id"]),
            objective=str(cfg["objective"]),
            optimization_event=str(cfg["optimization_event"]),
            landing_url=str(cfg["landing_url"]),
            run_days=int(cfg["run_days"]),
            geo=tuple(cfg.get("geo", [])),
            languages=tuple(cfg.get("languages", [])),
            creatives=creatives,
            guardrails=Guardrails.from_config(cfg["guardrails"]),
            source_dir=source_dir,
        )


@dataclass
class StopClock:
    """Pre-registered stopping rules, checked in a fixed precedence order.

    Pre-registered because a stopping rule invented while watching the numbers is not a
    stopping rule, it is a rationalization. Precedence-ordered because more than one
    trigger can be true at once and the reported reason should be stable across runs
    rather than depending on dict iteration order.
    """

    guardrails: Guardrails
    consecutive_collection_failures: int = 0
    _stopped: str | None = field(default=None)

    # Highest precedence first.
    TRIGGERS = ("budget", "unreadable_state", "collateral")

    def check(
        self,
        *,
        spent_minor: int,
        collection_failed: bool,
        collateral_signal: bool = False,
    ) -> str | None:
        if self._stopped is not None:
            return self._stopped

        self.consecutive_collection_failures = (
            self.consecutive_collection_failures + 1 if collection_failed else 0
        )

        if spent_minor >= self.guardrails.lifetime_budget_minor:
            self._stopped = "budget"
        elif (
            self.consecutive_collection_failures
            >= self.guardrails.max_collection_failures
        ):
            # A campaign whose results cannot be read is a campaign spending blind.
            self._stopped = "unreadable_state"
        elif collateral_signal:
            self._stopped = "collateral"
        return self._stopped

    @property
    def stopped(self) -> str | None:
        return self._stopped
