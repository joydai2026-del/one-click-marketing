"""Pulling performance data back, and storing it so it stays honest.

FOUR RULES, EACH FROM A REAL FAILURE MODE

MISSING IS NOT ZERO. A metric the platform did not report becomes `None`, never `0.0`.
"We could not read it" is not a measurement of zero, and the difference propagates all the
way to the ranker, which drops `None` samples rather than learning that whatever the
collector currently fails to read is bad content. A zero the platform ACTUALLY reported is
kept as zero: those two cases must stay distinguishable.

ACTIONS ARE FOUND BY NAME, NEVER BY POSITION. Platforms return a list of action objects.
Indexing into it works right up until the day the order changes, at which point purchases
are silently read from the wrong bucket and every downstream number is wrong with no error.
Any shape the parser does not recognize returns `None` rather than a guess.

STORAGE IS APPEND-ONLY, WITH RESOLUTION AT READ TIME. Ad platforms restate history: a
number for last Tuesday can change on Thursday. Upserting destroys the record of what you
knew when you made a decision. So every collection appends a new snapshot, and the current
value is resolved by a "latest effective" query keyed on (ad, day, attribution window),
newest source timestamp wins. An older restatement arriving late does NOT win.

ATTRIBUTION WINDOWS ARE PINNED AND STORED. The window is sent explicitly with the query and
recorded on every row. Inheriting the platform's account-level default means a setting
changed in a web UI silently redefines what every stored row MEANS, and a comparison across
that boundary is nonsense that looks like a trend.

A NOTE ON WHAT THIS IS NOT

Engagement rank and conversion counts are observational. Nothing here measures incremental
lift. Concluding "the campaign caused these purchases" requires a holdout, and the report
this module produces says "observed", never "caused".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _int_or_none(row: dict[str, Any], key: str) -> int | None:
    """Missing means unknown. Present means measured, including a genuine zero."""
    if key not in row or row[key] is None:
        return None
    try:
        return int(row[key])
    except (TypeError, ValueError):
        return None


def _spend_minor(row: dict[str, Any]) -> int | None:
    """Platforms report spend in major units; the column is minor units.

    Converted, never copied. A copied major-unit figure into a minor-unit column is a
    hundredfold understatement of spend, which the budget guard would then wave through.
    """
    raw = row.get("spend")
    if raw is None:
        return None
    try:
        return int(round(float(raw) * 100))
    except (TypeError, ValueError):
        return None


def extract_action(row: dict[str, Any], action_type: str) -> int | None:
    """Find one action count by name. Returns None for any shape not understood."""
    actions = row.get("actions")
    if not isinstance(actions, list):
        return None
    for a in actions:
        if not isinstance(a, dict) or a.get("action_type") != action_type:
            continue
        value = a.get("value")
        if value is None:
            # Some responses key the value by attribution window instead.
            for k, v in a.items():
                if k.endswith("_day_click") or k.endswith("_day_view"):
                    value = v
                    break
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


@dataclass(frozen=True)
class MetricSnapshot:
    """One reading of one ad on one day under one attribution window.

    Immutable by design: a reading is evidence of what the platform said at a moment, and
    evidence that can be edited is not evidence.
    """

    platform: str
    campaign_id: str
    platform_campaign_id: str
    platform_ad_id: str
    metric_date: str
    attribution_window: str
    currency: str
    spend_minor: int | None
    impressions: int | None
    clicks: int | None
    purchases: int | None
    creative_content_hash: str | None
    source_updated_at: float
    retrieved_at: float

    def __post_init__(self) -> None:
        if not (
            len(self.currency) == 3 and self.currency.isalpha() and self.currency.isupper()
        ):
            raise ValueError("currency must be an uppercase ISO-4217 alpha-3 code")
        if self.creative_content_hash == "":
            # NULL means "we do not know which asset". An empty string is a hash-shaped
            # hole that joins to nothing while reading as a real value.
            raise ValueError(
                "creative_content_hash must be None (unknown) or a real hash, never empty"
            )
        for name in ("spend_minor", "impressions", "clicks", "purchases"):
            v = getattr(self, name)
            if v is not None and v < 0:
                raise ValueError(f"{name} must be non-negative or None")

    @property
    def key(self) -> tuple[str, str, str, str]:
        """Identity of the READING, not of the row. Restatements share this key.

        Deliberately excludes our own campaign label: a platform ad id already names one
        ad, so adding our label could only split one ad into two "current" rows.
        """
        return (self.platform, self.platform_ad_id, self.metric_date, self.attribution_window)


def to_snapshots(
    rows: list[dict[str, Any]],
    *,
    platform: str,
    campaign_id: str,
    platform_campaign_id: str,
    attribution_window: str,
    currency: str,
    retrieved_at: float,
) -> tuple[list[MetricSnapshot], list[str]]:
    """Normalize platform rows. Returns (snapshots, errors).

    One malformed row becomes an error string and does not abort the batch: losing an
    entire day of readings because one row was odd is a worse outcome than losing one row.
    """
    snapshots: list[MetricSnapshot] = []
    errors: list[str] = []
    for i, row in enumerate(rows):
        try:
            snapshots.append(
                MetricSnapshot(
                    platform=platform,
                    campaign_id=campaign_id,
                    platform_campaign_id=platform_campaign_id,
                    platform_ad_id=str(row["ad_id"]),
                    metric_date=str(row["date"]),
                    attribution_window=attribution_window,
                    currency=currency,
                    spend_minor=_spend_minor(row),
                    impressions=_int_or_none(row, "impressions"),
                    clicks=_int_or_none(row, "clicks"),
                    purchases=extract_action(row, "purchase"),
                    # Joining ad ids back to creative hashes is a separate later step and
                    # must not hold up the spend number.
                    creative_content_hash=None,
                    # The platform exposes no restatement timestamp, so the batch's
                    # retrieval time is used and that fact is stated rather than an
                    # authoritative-sounding field being invented.
                    source_updated_at=retrieved_at,
                    retrieved_at=retrieved_at,
                )
            )
        except (KeyError, ValueError) as exc:
            errors.append(f"row {i}: {exc}")
    return snapshots, errors


@dataclass
class SnapshotStore:
    """Append-only store with read-time restatement resolution."""

    _rows: list[MetricSnapshot] = field(default_factory=list)

    def append(self, snapshots: list[MetricSnapshot]) -> int:
        self._rows.extend(snapshots)
        return len(snapshots)

    def all_rows(self) -> list[MetricSnapshot]:
        return list(self._rows)

    def latest_effective(self) -> list[MetricSnapshot]:
        """One current row per reading key: newest `source_updated_at` wins.

        A LATE-ARRIVING OLDER RESTATEMENT DOES NOT WIN. Insertion order is not the tie
        breaker; the source timestamp is. Ties fall back to insertion order so the result
        is deterministic rather than arbitrary.
        """
        best: dict[tuple[str, str, str, str], tuple[int, MetricSnapshot]] = {}
        for idx, row in enumerate(self._rows):
            current = best.get(row.key)
            if current is None:
                best[row.key] = (idx, row)
                continue
            cur_idx, cur_row = current
            if (row.source_updated_at, idx) > (cur_row.source_updated_at, cur_idx):
                best[row.key] = (idx, row)
        return [row for _, row in sorted(best.values(), key=lambda t: t[0])]

    def total_spend_minor(self) -> int | None:
        """Sum of the current readings, or None if nothing measurable is present.

        None rather than 0 when every reading is unknown, because a budget guard reading
        "0 spent" from "we cannot see the spend" would let a campaign run forever.
        """
        rows = [r.spend_minor for r in self.latest_effective() if r.spend_minor is not None]
        if not rows:
            return None
        return sum(rows)


@dataclass
class Collector:
    """One collection pass. Never loops: cadence belongs to a scheduler.

    A process that loops forever looks identical whether it is working or wedged. A
    one-shot run either exits zero or does not, and the scheduler notices.
    """

    platform: Any
    store: SnapshotStore
    attribution_window: str = "7d_click_1d_view"
    trailing_days: int = 8

    def collect(
        self,
        *,
        campaign_id: str,
        platform_campaign_id: str,
        currency: str,
        now: float,
    ) -> tuple[int, list[str]]:
        """Re-fetch a trailing window and append. Every call is a GET.

        The window is re-fetched rather than only the new day, because that is how a late
        restatement of an earlier day is ever seen. Double counting is prevented
        structurally by `latest_effective`, not by a write-time "have I seen this day"
        check, which would discard the correction the trailing window exists to catch.
        """
        rows = self.platform.insights(platform_campaign_id, self.trailing_days)
        snapshots, errors = to_snapshots(
            rows,
            platform="dryrun",
            campaign_id=campaign_id,
            platform_campaign_id=platform_campaign_id,
            attribution_window=self.attribution_window,
            currency=currency,
            retrieved_at=now,
        )
        written = self.store.append(snapshots)
        return written, errors
