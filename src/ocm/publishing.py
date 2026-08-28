"""The publish state machine, the schedule policy, and at-most-once publishing.

THE STATE MACHINE

    drafted -> evaluated -> approved -> publishing -> posting -> published
                        \\-> rejected (terminal)

`transition` is a lookup in an explicit table and raises on any move not in it. In
particular `evaluated -> published` is impossible: nothing reaches a channel without
passing through an approval.

WHY `posting` EXISTS

It is the phase marker written and COMMITTED immediately BEFORE the network call, and
cleared after. Without it, a crash mid-publish is indistinguishable from a crash before
publish, and the recovery logic has to guess. Guessing wrong in one direction loses a post;
guessing wrong in the other direction posts twice. With it, a row found in `posting` is
known to have possibly landed, and recovery reconciles against the channel instead of
retrying blindly.

INDETERMINATE IS NOT FAILED

A publish whose outcome is unknown, typically a timeout, leaves the row parked in
`publishing` and is NEVER auto-retried. On an organic channel that costs a duplicate post;
on a paid channel it costs a double charge. Parking requires a human to look, which is the
correct cost for an ambiguous outcome.

AN `ok` RESULT WITHOUT AN EXTERNAL ID IS REFUSED

A channel reporting success but no id has published something the system can never find,
measure, or delete. That is worse than a failure, so it is treated as one.

THE SCHEDULE POLICY

One publish per channel per window, enforced in ONE place. Not two places that agree today:
the version this distills had the same cap expressed in two files, and keeping them
agreeing was manual. The cap is counted from a durable append-only log of CONFIRMED posts,
so a skipped or crashed tick does not consume the day's slot.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import Draft, PublishRecord, Stage

_ALLOWED: dict[Stage, frozenset[Stage]] = {
    Stage.DRAFTED: frozenset({Stage.EVALUATED, Stage.REJECTED}),
    Stage.EVALUATED: frozenset({Stage.APPROVED, Stage.REJECTED}),
    Stage.APPROVED: frozenset({Stage.PUBLISHED, Stage.REJECTED}),
    Stage.PUBLISHED: frozenset({Stage.COLLECTED}),
    Stage.COLLECTED: frozenset(),
    Stage.REJECTED: frozenset(),
}


class GateError(Exception):
    """An illegal state transition was attempted."""


class IndeterminateOutcome(Exception):
    """A publish may or may not have landed. Do not retry; park for a human."""


def transition(current: Stage, target: Stage) -> Stage:
    allowed = _ALLOWED.get(current, frozenset())
    if target not in allowed:
        raise GateError(
            f"illegal transition {current.value} -> {target.value}; "
            f"allowed: {sorted(s.value for s in allowed) or 'none (terminal)'}"
        )
    return target


@dataclass
class PostLog:
    """Append-only JSONL of CONFIRMED posts. The durable source of the daily cap.

    Confirmed only: a row is written after a channel returned a real external id. A
    skipped or failed tick leaves no entry and therefore does not consume the slot.

    Malformed lines are skipped by the counters rather than raising, because one corrupt
    line must not make the cap uncountable and thereby block all publishing. The residual
    gap is stated rather than hidden: a crash after a confirmed post but before the log
    write under-counts by one. That is bounded by the cap and can never double-post.
    """

    path: Path | None = None
    _memory: list[dict] = field(default_factory=list)

    def _append(self, entry: dict) -> None:
        if self.path is None:
            self._memory.append(entry)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
            # Flushed and fsynced BEFORE the caller makes its network call. An intent row
            # sitting in a buffer when the process dies is an intent row that never
            # existed, which is the whole failure this row is meant to make visible.
            fh.flush()
            os.fsync(fh.fileno())

    def record_intent(self, draft_id: str, channel: str, *, now: float) -> None:
        """Write the `posting` marker BEFORE the channel call, and commit it.

        This is what makes a crash unambiguous. A dangling intent row with no matching
        confirmation means the outcome is UNKNOWN: the post may or may not have landed,
        and recovery must reconcile against the channel rather than blindly retry.
        `dangling_intents` finds them.
        """
        self._append(
            {"phase": "posting", "draft_id": draft_id, "channel": channel, "at": now}
        )

    def record(self, record: PublishRecord) -> None:
        """Write the confirmed post. Only rows written here count toward the cap."""
        self._append(
            {
                "phase": "published",
                "publish_id": record.publish_id,
                "draft_id": record.draft_id,
                "channel": record.channel,
                "external_id": record.external_id,
                "published_at": record.published_at,
            }
        )

    def dangling_intents(self) -> list[dict]:
        """Intent rows with no matching confirmation: the possibly-published set.

        These are exactly the rows a human must look at. Nothing in this codebase retries
        them, because retrying an unknown outcome is how one post becomes two.
        """
        confirmed = {
            e.get("draft_id") for e in self.entries() if e.get("phase") == "published"
        }
        return [
            e
            for e in self.entries()
            if e.get("phase") == "posting" and e.get("draft_id") not in confirmed
        ]

    def entries(self) -> list[dict]:
        if self.path is None:
            return list(self._memory)
        if not self.path.is_file():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # skip, never raise: see docstring
            if isinstance(obj, dict):
                out.append(obj)
        return out

    def count_in_window(self, channel: str, *, now: float, window_hours: int) -> int:
        # The boundary is EXCLUSIVE: a post exactly `window_hours` old is outside the
        # window. This is not a style choice. The canonical use of this cap is a
        # once-daily job with a 24h window, where yesterday's post is exactly 24h old at
        # the moment today's tick runs. An inclusive boundary counts it, the cap refuses,
        # and a "once a day" schedule quietly becomes once every OTHER day, forever, with
        # every skip logged as correct cap enforcement.
        cutoff = now - window_hours * 3600
        return sum(
            1
            for e in self.confirmed()
            if e.get("channel") == channel and float(e.get("published_at", 0)) > cutoff
        )

    def confirmed(self) -> list[dict]:
        """Only rows for posts a channel actually acknowledged.

        The cap and the rotation offset both read THIS, never `entries()`. Counting an
        unconfirmed intent row toward the cap would let a failed publish consume the day's
        slot, so one channel error would silently cost a day of output.
        """
        return [e for e in self.entries() if e.get("phase") == "published"]

    def total(self, channel: str | None = None) -> int:
        """Confirmed-post count. Doubles as the durable style rotation offset.

        Advancing the offset only on a CONFIRMED post means a skipped tick reuses its
        style slot rather than burning it, and a crash-refire replays the same style.
        """
        if channel is None:
            return len(self.confirmed())
        return sum(1 for e in self.confirmed() if e.get("channel") == channel)


@dataclass
class SchedulePolicy:
    """One cap, one place.

    Args:
        max_per_window: confirmed posts allowed per channel per window.
        window_hours: the window length.
    """

    max_per_window: int = 1
    window_hours: int = 24

    def __post_init__(self) -> None:
        # A zero or negative window makes the cutoff equal to or later than `now`, so no
        # post is ever inside the window, `count_in_window` always returns 0, and the cap
        # silently permits unlimited posting. A cap that turns itself off when misconfigured
        # is worse than no cap, because the config still says the limit is one.
        if self.window_hours <= 0:
            raise ValueError("window_hours must be positive")
        if self.max_per_window < 0:
            raise ValueError("max_per_window must be non-negative")

    @classmethod
    def from_config(cls, cfg: dict) -> SchedulePolicy:
        return cls(
            max_per_window=int(cfg.get("max_per_window", 1)),
            window_hours=int(cfg.get("window_hours", 24)),
        )

    def may_publish(self, log: PostLog, channel: str, *, now: float) -> tuple[bool, str]:
        n = log.count_in_window(channel, now=now, window_hours=self.window_hours)
        if n >= self.max_per_window:
            return False, (
                f"channel {channel} already published {n} time(s) in the last "
                f"{self.window_hours}h, cap is {self.max_per_window}"
            )
        return True, ""


@dataclass
class Publisher:
    """Drives one draft through the publish phases and records the result.

    The phase list is exposed for tests to assert the ORDER, in particular that `posting`
    is recorded before the channel call rather than after.
    """

    log: PostLog
    phases: list[str] = field(default_factory=list)

    def publish(self, adapter, draft: Draft, *, now: float | None = None) -> PublishRecord:
        """Drive one draft to a channel and record the confirmed post.

        `now` is the publisher's clock, and the publisher stamps `published_at` with it.

        THE CLOCK BELONGS TO THE PUBLISHER, NOT THE ADAPTER. An adapter is free to
        construct a record with a default wall-clock timestamp, but the durable log and
        the schedule cap are read against the caller's clock, so the two must be the same
        clock or the cap silently stops working. That is not a hypothetical: with an
        adapter-stamped wall-clock time and a caller-injected `now`, every confirmed post
        falls outside the window, `count_in_window` returns 0, and the one-post-per-window
        cap never fires at all. It only appears to work when the injected clock happens to
        agree with the wall clock.

        Restamping here also means a simulated run and a real run exercise the identical
        code path, which is the only way the cap is testable at all.
        """
        errors = adapter.validate(draft)
        if errors:
            raise GateError(
                f"channel {adapter.name} rejected the draft: "
                + "; ".join(f"{e.field}: {e.detail}" for e in errors)
            )

        self.phases.append("publishing")

        # Committed and fsynced BEFORE the network call. A crash after this point leaves a
        # dangling intent row, so recovery KNOWS the outcome is unknown and reconciles
        # against the channel instead of retrying blindly.
        self.phases.append("posting")
        self.log.record_intent(
            draft.draft_id, adapter.name, now=now if now is not None else draft.created_at
        )

        try:
            record = adapter.publish(draft)
        except TimeoutError as exc:
            # The intent row is deliberately LEFT DANGLING. Removing it here would erase
            # the only evidence that something may have been published, which is exactly
            # the ambiguity the row exists to preserve.
            raise IndeterminateOutcome(
                "the publish outcome is unknown; the intent row is left dangling for a "
                "human to reconcile, and is never auto-retried"
            ) from exc

        if not record.external_id:
            # Published something the system can never find, measure, or delete.
            raise GateError(
                f"channel {adapter.name} reported success without an external id; "
                f"refusing to record an unfindable post as published"
            )

        if now is not None:
            record = replace(record, published_at=now)

        self.phases.append("published")
        self.log.record(record)
        return record


def window_start(now: float, window_hours: int) -> str:
    dt = datetime.fromtimestamp(now, tz=UTC) - timedelta(hours=window_hours)
    return dt.isoformat()
