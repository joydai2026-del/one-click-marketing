"""The publish state machine, at-most-once publishing, and the one-place schedule cap."""

from __future__ import annotations

import json

import pytest
from conftest import FIXED_NOW, RecordingAdapter, make_draft

from ocm.models import PublishRecord, Stage, new_id
from ocm.publishing import (
    GateError,
    IndeterminateOutcome,
    PostLog,
    Publisher,
    SchedulePolicy,
    transition,
    window_start,
)

BODY = "a draft body long enough to be a plausible piece of content for a channel"

LEGAL_MOVES = [
    (Stage.DRAFTED, Stage.EVALUATED),
    (Stage.DRAFTED, Stage.REJECTED),
    (Stage.EVALUATED, Stage.APPROVED),
    (Stage.EVALUATED, Stage.REJECTED),
    (Stage.APPROVED, Stage.PUBLISHED),
    (Stage.APPROVED, Stage.REJECTED),
    (Stage.PUBLISHED, Stage.COLLECTED),
]

ALL_STAGES = list(Stage)
ILLEGAL_MOVES = [
    (a, b) for a in ALL_STAGES for b in ALL_STAGES if (a, b) not in LEGAL_MOVES
]


# --------------------------------------------------------------------------------------
# the transition table
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("current,target", LEGAL_MOVES, ids=lambda s: s.value)
def test_every_legal_move_is_allowed_and_returns_the_target(current, target):
    assert transition(current, target) is target


@pytest.mark.parametrize("current,target", ILLEGAL_MOVES, ids=lambda s: s.value)
def test_every_move_outside_the_table_raises(current, target):
    """The table is exhaustive by construction here: anything not enumerated as legal is
    asserted to raise, so adding an edge to the code without adding it to LEGAL_MOVES
    fails this test rather than sliding through unnoticed.
    """
    with pytest.raises(GateError):
        transition(current, target)


def test_evaluated_to_published_is_impossible():
    """Nothing reaches a channel without passing through an approval. This is the single
    most load-bearing edge in the table, so it gets its own named test.
    """
    with pytest.raises(GateError, match="illegal transition evaluated -> published"):
        transition(Stage.EVALUATED, Stage.PUBLISHED)


def test_drafted_to_published_is_impossible():
    with pytest.raises(GateError):
        transition(Stage.DRAFTED, Stage.PUBLISHED)


@pytest.mark.parametrize("terminal", [Stage.COLLECTED, Stage.REJECTED])
def test_terminal_states_have_no_exits(terminal):
    for target in ALL_STAGES:
        with pytest.raises(GateError):
            transition(terminal, target)


def test_a_terminal_states_error_says_it_is_terminal():
    with pytest.raises(GateError, match="none \\(terminal\\)"):
        transition(Stage.REJECTED, Stage.DRAFTED)


def test_the_error_names_what_would_have_been_allowed():
    with pytest.raises(GateError, match="approved"):
        transition(Stage.EVALUATED, Stage.COLLECTED)


def test_no_stage_may_move_to_itself():
    for stage in ALL_STAGES:
        with pytest.raises(GateError):
            transition(stage, stage)


# --------------------------------------------------------------------------------------
# the publisher
# --------------------------------------------------------------------------------------


def test_posting_is_recorded_before_the_channel_call():
    """`posting` is the phase marker written and committed immediately BEFORE the network
    call. Without it, a crash mid-publish is indistinguishable from a crash before
    publish, and recovery has to guess: one way loses a post, the other posts twice.

    Asserting on the FINAL phase list would not prove ordering, so the adapter snapshots
    the list at the moment it is called.
    """
    log = PostLog()
    publisher = Publisher(log=log)
    adapter = RecordingAdapter()
    adapter.publisher = publisher

    publisher.publish(adapter, make_draft(BODY))

    assert adapter.phases_at_call == ["publishing", "posting"]
    assert publisher.phases == ["publishing", "posting", "published"]


def test_published_is_recorded_only_after_the_channel_answered():
    log = PostLog()
    publisher = Publisher(log=log)
    adapter = RecordingAdapter()
    adapter.publisher = publisher
    publisher.publish(adapter, make_draft(BODY))
    assert adapter.phases_at_call is not None
    assert "published" not in adapter.phases_at_call


def test_a_validation_failure_stops_before_any_phase_is_recorded():
    """A draft the channel cannot carry never becomes a possibly-published row."""
    from ocm.channels.base import ValidationError

    publisher = Publisher(log=PostLog())
    adapter = RecordingAdapter(validation_errors=[ValidationError("title", "required")])

    with pytest.raises(GateError, match="rejected the draft"):
        publisher.publish(adapter, make_draft(BODY))

    assert publisher.phases == []
    assert adapter.publish_calls == 0


def test_a_success_with_an_empty_external_id_is_refused():
    """A channel reporting success but no id has published something the system can never
    find, measure, or delete. That is worse than a failure, so it is treated as one."""
    log = PostLog()
    publisher = Publisher(log=log)
    adapter = RecordingAdapter(external_id="")

    with pytest.raises(GateError, match="without an external id"):
        publisher.publish(adapter, make_draft(BODY))

    assert log.total() == 0
    assert "published" not in publisher.phases


def test_a_timeout_raises_indeterminate_outcome_and_is_not_retried():
    """A publish whose outcome is unknown is NEVER auto-retried. On an organic channel a
    blind retry costs a duplicate post; on a paid channel it costs a double charge."""
    log = PostLog()
    adapter = RecordingAdapter(raise_timeout=True)

    with pytest.raises(IndeterminateOutcome, match="parked for a human"):
        Publisher(log=log).publish(adapter, make_draft(BODY))

    assert adapter.publish_calls == 1
    assert log.total() == 0


def test_indeterminate_outcome_is_not_a_gate_error():
    """They call for opposite responses, so a caller must be able to tell them apart:
    a GateError means the draft was refused, an IndeterminateOutcome means look at it."""
    assert not issubclass(IndeterminateOutcome, GateError)
    assert not issubclass(GateError, IndeterminateOutcome)


def test_a_confirmed_publish_is_recorded_in_the_log():
    log = PostLog()
    record = Publisher(log=log).publish(RecordingAdapter(name="substack"), make_draft(BODY))
    assert log.total() == 1
    assert log.entries()[0]["external_id"] == record.external_id
    assert log.entries()[0]["channel"] == "substack"


def test_the_publisher_stamps_published_at_from_its_own_clock():
    """THE CLOCK BELONGS TO THE PUBLISHER, NOT THE ADAPTER.

    The durable log is WRITTEN by the publisher and READ by the schedule cap. If the
    adapter stamps a wall-clock time while the cap is evaluated against a caller-injected
    `now`, the two disagree, every confirmed post falls outside its own window,
    `count_in_window` returns 0, and the one-post-per-window cap never fires at all. It
    only appears to work when the injected clock happens to match the wall clock.
    """
    log = PostLog()
    adapter = RecordingAdapter()
    # The adapter deliberately reports a DIFFERENT instant, so a pass here cannot be an
    # accident of the two clocks agreeing.
    assert FIXED_NOW != FIXED_NOW + 999_999

    record = Publisher(log=log).publish(adapter, make_draft(BODY), now=FIXED_NOW + 999_999)

    assert record.published_at == FIXED_NOW + 999_999
    assert log.entries()[0]["published_at"] == FIXED_NOW + 999_999


def test_a_publish_with_no_clock_keeps_the_adapters_own_timestamp():
    """`now` is optional, so a caller that has no simulated clock is unaffected."""
    log = PostLog()
    record = Publisher(log=log).publish(RecordingAdapter(), make_draft(BODY), now=None)
    assert record.published_at == FIXED_NOW
    assert log.entries()[0]["published_at"] == FIXED_NOW


def test_the_publisher_clock_and_the_schedule_cap_read_the_same_log():
    """The two halves joined up: publish at `now`, then ask the cap at the same `now`."""
    log = PostLog()
    policy = SchedulePolicy(max_per_window=1, window_hours=24)
    simulated = FIXED_NOW + 10 * 86_400

    assert policy.may_publish(log, "fake", now=simulated)[0] is True
    Publisher(log=log).publish(RecordingAdapter(), make_draft(BODY), now=simulated)
    assert policy.may_publish(log, "fake", now=simulated)[0] is False
    assert policy.may_publish(log, "fake", now=simulated + 25 * 3600)[0] is True


def test_a_refused_publish_stamps_nothing_because_it_records_nothing():
    """The restamp happens after the external-id check, so an unfindable post leaves the
    log untouched rather than leaving a correctly-timestamped phantom entry."""
    log = PostLog()
    with pytest.raises(GateError):
        Publisher(log=log).publish(
            RecordingAdapter(external_id=""), make_draft(BODY), now=FIXED_NOW
        )
    assert log.entries() == []


# --------------------------------------------------------------------------------------
# the post log
# --------------------------------------------------------------------------------------


def record(channel="substack", at=FIXED_NOW, external_id="ext") -> PublishRecord:
    return PublishRecord(
        publish_id=new_id("pub"),
        draft_id="d1",
        channel=channel,
        content_hash="h",
        external_id=external_id,
        external_url="https://example.invalid/x",
        dry_run=True,
        published_at=at,
    )


def test_the_log_persists_to_jsonl_and_reads_back(tmp_path):
    path = tmp_path / "nested" / "posts.jsonl"
    log = PostLog(path=path)
    log.record(record())
    log.record(record(channel="x"))

    assert path.is_file()
    assert PostLog(path=path).total() == 2
    assert PostLog(path=path).total("x") == 1


def test_a_missing_log_file_reads_as_empty_rather_than_raising(tmp_path):
    """A first run has no log, and that must not look like a failure."""
    assert PostLog(path=tmp_path / "never-written.jsonl").entries() == []


def test_malformed_jsonl_lines_are_skipped_without_raising(tmp_path):
    """One corrupt line must not make the cap uncountable and thereby block ALL
    publishing. Failing open on counting would be worse in the other direction, so the
    residual gap is that a corrupt line under-counts, which is bounded by the cap and can
    never double-post.
    """
    path = tmp_path / "posts.jsonl"
    log = PostLog(path=path)
    log.record(record(external_id="good-1"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
        fh.write("\n")
        fh.write(json.dumps(["a list, not an object"]) + "\n")
        fh.write(json.dumps("a bare string") + "\n")
    log.record(record(external_id="good-2"))

    entries = PostLog(path=path).entries()
    assert [e["external_id"] for e in entries] == ["good-1", "good-2"]
    assert PostLog(path=path).total() == 2


def test_count_in_window_respects_the_window():
    log = PostLog()
    log.record(record(at=FIXED_NOW - 25 * 3600))  # just outside 24h
    log.record(record(at=FIXED_NOW - 23 * 3600))  # just inside
    log.record(record(at=FIXED_NOW))

    assert log.count_in_window("substack", now=FIXED_NOW, window_hours=24) == 2
    assert log.count_in_window("substack", now=FIXED_NOW, window_hours=48) == 3
    assert log.count_in_window("substack", now=FIXED_NOW, window_hours=1) == 1


def test_count_in_window_is_per_channel():
    log = PostLog()
    log.record(record(channel="substack"))
    log.record(record(channel="x"))
    log.record(record(channel="x"))
    assert log.count_in_window("substack", now=FIXED_NOW, window_hours=24) == 1
    assert log.count_in_window("x", now=FIXED_NOW, window_hours=24) == 2
    assert log.count_in_window("unknown", now=FIXED_NOW, window_hours=24) == 0


def test_the_window_boundary_is_exclusive_so_a_daily_cadence_is_not_halved():
    """A post exactly `window_hours` old is OUTSIDE the window.

    REGRESSION GUARD. With an inclusive boundary, a once-daily job on a 24h window finds
    yesterday's post exactly 24h old at today's tick, counts it, and refuses. "Once a day"
    silently becomes once every other day, forever, with every skip logged as correct cap
    enforcement.
    """
    log = PostLog()
    log.record(record(at=FIXED_NOW - 24 * 3600))
    assert log.count_in_window("substack", now=FIXED_NOW, window_hours=24) == 0

    # One second inside the window is still inside it.
    log2 = PostLog()
    log2.record(record(at=FIXED_NOW - 24 * 3600 + 1))
    assert log2.count_in_window("substack", now=FIXED_NOW, window_hours=24) == 1


def test_total_advances_only_on_confirmed_posts():
    """Confirmed only: a skipped or failed tick leaves no entry and therefore does not
    consume the slot, and (since `total` doubles as the rotation offset) does not burn a
    style position either.
    """
    log = PostLog()
    publisher = Publisher(log=log)

    publisher.publish(RecordingAdapter(external_id="ext-ok"), make_draft(BODY))
    assert log.total() == 1

    with pytest.raises(GateError):
        Publisher(log=log).publish(RecordingAdapter(external_id=""), make_draft(BODY))
    assert log.total() == 1

    with pytest.raises(IndeterminateOutcome):
        Publisher(log=log).publish(RecordingAdapter(raise_timeout=True), make_draft(BODY))
    assert log.total() == 1


def test_total_is_per_channel_or_global():
    log = PostLog()
    log.record(record(channel="substack"))
    log.record(record(channel="x"))
    assert log.total() == 2
    assert log.total("substack") == 1


# --------------------------------------------------------------------------------------
# the schedule policy
# --------------------------------------------------------------------------------------


def test_a_channel_with_no_posts_in_the_window_may_publish():
    allowed, reason = SchedulePolicy().may_publish(PostLog(), "substack", now=FIXED_NOW)
    assert allowed is True
    assert reason == ""


def test_the_cap_is_enforced_and_the_refusal_explains_itself():
    log = PostLog()
    log.record(record(channel="substack", at=FIXED_NOW))
    allowed, reason = SchedulePolicy(max_per_window=1, window_hours=24).may_publish(
        log, "substack", now=FIXED_NOW
    )
    assert allowed is False
    assert "already published 1 time(s)" in reason
    assert "cap is 1" in reason


def test_the_cap_is_per_channel():
    log = PostLog()
    log.record(record(channel="substack", at=FIXED_NOW))
    policy = SchedulePolicy(max_per_window=1)
    assert policy.may_publish(log, "substack", now=FIXED_NOW)[0] is False
    assert policy.may_publish(log, "x", now=FIXED_NOW)[0] is True


def test_the_cap_lifts_once_the_window_has_passed():
    log = PostLog()
    log.record(record(channel="substack", at=FIXED_NOW))
    policy = SchedulePolicy(max_per_window=1, window_hours=24)
    assert policy.may_publish(log, "substack", now=FIXED_NOW + 25 * 3600)[0] is True


@pytest.mark.parametrize(
    "cap,posted,allowed", [(1, 0, True), (1, 1, False), (3, 2, True), (3, 3, False)]
)
def test_the_cap_value_is_the_only_thing_that_decides(cap, posted, allowed):
    log = PostLog()
    for _ in range(posted):
        log.record(record(channel="substack", at=FIXED_NOW))
    policy = SchedulePolicy(max_per_window=cap)
    assert policy.may_publish(log, "substack", now=FIXED_NOW)[0] is allowed


def test_a_zero_cap_stops_a_channel_entirely():
    """A config-only kill switch for one channel, with no code change."""
    policy = SchedulePolicy(max_per_window=0)
    assert policy.may_publish(PostLog(), "substack", now=FIXED_NOW)[0] is False


def test_from_config_reads_the_cap_and_the_window():
    policy = SchedulePolicy.from_config({"max_per_window": 4, "window_hours": 6})
    assert (policy.max_per_window, policy.window_hours) == (4, 6)


def test_from_config_defaults_to_one_per_day():
    policy = SchedulePolicy.from_config({})
    assert (policy.max_per_window, policy.window_hours) == (1, 24)


def test_the_cap_lives_in_exactly_one_place():
    """The version this distills had the same cap expressed in two files, and keeping them
    agreeing was manual. Any other module deciding "may this publish" is the regression.
    """
    import inspect

    import ocm.loop as loop_mod
    import ocm.publishing as pub_mod

    assert "may_publish" in inspect.getsource(pub_mod.SchedulePolicy)
    loop_src = inspect.getsource(loop_mod)
    assert loop_src.count("may_publish") == 1
    assert "max_per_window" not in loop_src


def test_window_start_is_the_utc_iso_instant_one_window_ago():
    assert window_start(FIXED_NOW, 24).startswith("2027-01-14T")
