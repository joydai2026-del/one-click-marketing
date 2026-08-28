"""Pulling performance data back, and storing it so it stays honest.

Four rules, each from a real failure mode: missing is not zero, actions are found by name,
storage is append-only with resolution at read time, and spend is converted rather than
copied.
"""

from __future__ import annotations

import dataclasses

import pytest

from ocm.paid.collector import (
    Collector,
    MetricSnapshot,
    SnapshotStore,
    extract_action,
    to_snapshots,
)
from ocm.paid.platform import DryRunPlatform

T0 = 1_800_000_000.0


def snapshot(**overrides) -> MetricSnapshot:
    kwargs = dict(
        platform="dryrun",
        campaign_id="camp-1",
        platform_campaign_id="pcid-1",
        platform_ad_id="ad-1",
        metric_date="2026-01-01",
        attribution_window="7d_click_1d_view",
        currency="USD",
        spend_minor=100,
        impressions=1000,
        clicks=10,
        purchases=1,
        creative_content_hash=None,
        source_updated_at=T0,
        retrieved_at=T0,
    )
    kwargs.update(overrides)
    return MetricSnapshot(**kwargs)


def row(**overrides) -> dict:
    base = {
        "ad_id": "ad-1",
        "date": "2026-01-01",
        "spend": 1.50,
        "impressions": 1000,
        "clicks": 10,
        "actions": [{"action_type": "purchase", "value": "3"}],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------------------
# actions are found by name, never by position
# --------------------------------------------------------------------------------------


def test_extract_action_finds_the_action_by_name():
    assert extract_action(row(), "purchase") == 3


def test_extract_action_is_unaffected_by_the_order_of_the_actions_list():
    """Indexing into the list works right up until the day the order changes, at which
    point purchases are silently read from the wrong bucket and every downstream number is
    wrong with no error anywhere.
    """
    actions = [
        {"action_type": "link_click", "value": "77"},
        {"action_type": "purchase", "value": "3"},
        {"action_type": "add_to_cart", "value": "12"},
    ]
    forward = row(actions=actions)
    reordered = row(actions=list(reversed(actions)))

    assert extract_action(forward, "purchase") == extract_action(reordered, "purchase") == 3
    assert extract_action(reordered, "link_click") == 77


def test_extract_action_returns_none_for_an_action_type_that_is_absent():
    assert extract_action(row(), "add_to_cart") is None


@pytest.mark.parametrize(
    "actions,why",
    [
        (None, "key present but null"),
        ("purchase=3", "a string instead of a list"),
        ({"purchase": 3}, "a mapping instead of a list"),
        ([["purchase", 3]], "a list of lists"),
        ([{"action_type": "purchase"}], "no value on the matching action"),
        ([{"action_type": "purchase", "value": None}], "an explicit null value"),
        ([{"action_type": "purchase", "value": "not-a-number"}], "an unparseable value"),
    ],
)
def test_extract_action_returns_none_for_any_shape_it_does_not_understand(actions, why):
    """A guess here is a number nobody can trace, so an unrecognized shape is unknown."""
    assert extract_action(row(actions=actions), "purchase") is None, why


def test_extract_action_returns_none_when_the_actions_key_is_missing_entirely():
    r = row()
    del r["actions"]
    assert extract_action(r, "purchase") is None


@pytest.mark.parametrize("key", ["7_day_click", "1_day_view", "28_day_click"])
def test_extract_action_falls_back_to_an_attribution_window_keyed_value(key):
    """Some responses key the value by attribution window instead of by `value`."""
    assert extract_action(row(actions=[{"action_type": "purchase", key: "5"}]), "purchase") == 5


def test_an_unrecognized_window_key_spelling_reads_as_unknown_rather_than_as_a_guess():
    """The recognized spellings are `*_day_click` and `*_day_view`. Anything else, such as
    a compact `7d_click`, yields None. Fail-closed is the right direction: a wrong number
    here is untraceable, while a None is visible all the way to the ranker, which drops it.
    """
    compact = row(actions=[{"action_type": "purchase", "7d_click": "5"}])
    assert extract_action(compact, "purchase") is None


def test_a_genuine_zero_action_count_stays_zero():
    assert extract_action(row(actions=[{"action_type": "purchase", "value": "0"}]), "purchase") == 0


# --------------------------------------------------------------------------------------
# missing is not zero
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["impressions", "clicks"])
def test_a_missing_metric_becomes_none_not_zero(field):
    """"We could not read it" is not a measurement of zero, and the difference propagates
    to the ranker, which drops unmeasured samples rather than learning that whatever the
    collector currently fails to read is bad content.
    """
    r = row()
    del r[field]
    snaps, errors = to_snapshots(
        [r], platform="p", campaign_id="c", platform_campaign_id="pc",
        attribution_window="w", currency="USD", retrieved_at=T0,
    )
    assert errors == []
    assert getattr(snaps[0], field) is None


@pytest.mark.parametrize("field", ["impressions", "clicks"])
def test_an_explicit_null_metric_also_becomes_none(field):
    snaps, _ = to_snapshots(
        [row(**{field: None})], platform="p", campaign_id="c", platform_campaign_id="pc",
        attribution_window="w", currency="USD", retrieved_at=T0,
    )
    assert getattr(snaps[0], field) is None


@pytest.mark.parametrize("field", ["impressions", "clicks"])
def test_a_genuine_zero_metric_stays_zero_and_stays_distinguishable(field):
    snaps, _ = to_snapshots(
        [row(**{field: 0})], platform="p", campaign_id="c", platform_campaign_id="pc",
        attribution_window="w", currency="USD", retrieved_at=T0,
    )
    value = getattr(snaps[0], field)
    assert value == 0
    assert value is not None


def test_an_unparseable_metric_becomes_none_rather_than_raising():
    snaps, errors = to_snapshots(
        [row(impressions="lots")], platform="p", campaign_id="c", platform_campaign_id="pc",
        attribution_window="w", currency="USD", retrieved_at=T0,
    )
    assert errors == []
    assert snaps[0].impressions is None


# --------------------------------------------------------------------------------------
# spend is converted, never copied
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "major,minor",
    [(1.50, 150), (0.01, 1), (0, 0), (250.0, 25_000), (9.99, 999), ("3.25", 325)],
)
def test_spend_is_converted_from_major_to_minor_units(major, minor):
    """A copied major-unit figure in a minor-unit column is a hundredfold understatement
    of spend, which the budget guard would then wave through."""
    snaps, _ = to_snapshots(
        [row(spend=major)], platform="p", campaign_id="c", platform_campaign_id="pc",
        attribution_window="w", currency="USD", retrieved_at=T0,
    )
    assert snaps[0].spend_minor == minor


def test_missing_spend_is_none_not_zero():
    r = row()
    del r["spend"]
    snaps, _ = to_snapshots(
        [r], platform="p", campaign_id="c", platform_campaign_id="pc",
        attribution_window="w", currency="USD", retrieved_at=T0,
    )
    assert snaps[0].spend_minor is None


def test_unparseable_spend_is_none_not_zero():
    snaps, _ = to_snapshots(
        [row(spend="unknown")], platform="p", campaign_id="c", platform_campaign_id="pc",
        attribution_window="w", currency="USD", retrieved_at=T0,
    )
    assert snaps[0].spend_minor is None


# --------------------------------------------------------------------------------------
# snapshot validation
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("currency", ["usd", "US", "USDD", ""])
def test_a_snapshot_rejects_a_malformed_currency(currency):
    with pytest.raises(ValueError, match="ISO-4217"):
        snapshot(currency=currency)


def test_a_snapshot_rejects_an_empty_string_creative_hash():
    """NULL means "we do not know which asset". An empty string is a hash-shaped hole that
    joins to nothing while reading as a real value."""
    with pytest.raises(ValueError, match="never empty"):
        snapshot(creative_content_hash="")


def test_a_snapshot_accepts_none_or_a_real_hash_for_the_creative():
    assert snapshot(creative_content_hash=None).creative_content_hash is None
    assert snapshot(creative_content_hash="a" * 64).creative_content_hash == "a" * 64


@pytest.mark.parametrize("field", ["spend_minor", "impressions", "clicks", "purchases"])
def test_a_snapshot_rejects_a_negative_metric(field):
    """A negative counter is a parse error wearing a number's clothes."""
    with pytest.raises(ValueError, match="non-negative"):
        snapshot(**{field: -1})


def test_a_snapshot_is_immutable():
    """A reading is evidence of what the platform said at a moment, and evidence that can
    be edited is not evidence."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot().spend_minor = 999  # type: ignore[misc]


def test_the_reading_key_identifies_the_reading_not_the_row():
    """Restatements share this key, which is what lets read-time resolution pick between
    them. It deliberately excludes our own campaign label: a platform ad id already names
    one ad, so adding our label could only split one ad into two "current" rows.
    """
    a = snapshot(campaign_id="our-label-v1")
    b = snapshot(campaign_id="our-label-v2")
    assert a.key == b.key
    assert a.key == ("dryrun", "ad-1", "2026-01-01", "7d_click_1d_view")
    assert snapshot(attribution_window="1d_click").key != a.key


# --------------------------------------------------------------------------------------
# to_snapshots batch behavior
# --------------------------------------------------------------------------------------


def test_one_malformed_row_does_not_abort_the_batch():
    """Losing an entire day of readings because one row was odd is a worse outcome than
    losing one row."""
    rows = [row(ad_id="ad-1"), {"date": "2026-01-02"}, row(ad_id="ad-3")]
    snaps, errors = to_snapshots(
        rows, platform="p", campaign_id="c", platform_campaign_id="pc",
        attribution_window="w", currency="USD", retrieved_at=T0,
    )
    assert [s.platform_ad_id for s in snaps] == ["ad-1", "ad-3"]
    assert len(errors) == 1
    assert errors[0].startswith("row 1:")


def test_a_row_that_fails_validation_is_reported_as_an_error(caplog):
    rows = [row(impressions=-5)]
    snaps, errors = to_snapshots(
        rows, platform="p", campaign_id="c", platform_campaign_id="pc",
        attribution_window="w", currency="USD", retrieved_at=T0,
    )
    assert snaps == []
    assert len(errors) == 1 and "non-negative" in errors[0]


def test_the_attribution_window_is_recorded_on_every_row():
    """Inheriting the platform's account-level default means a setting changed in a web UI
    silently redefines what every stored row MEANS."""
    snaps, _ = to_snapshots(
        [row(), row(ad_id="ad-2")], platform="p", campaign_id="c", platform_campaign_id="pc",
        attribution_window="1d_click", currency="USD", retrieved_at=T0,
    )
    assert {s.attribution_window for s in snaps} == {"1d_click"}


def test_the_creative_hash_is_left_unknown_rather_than_guessed():
    """Joining ad ids back to creative hashes is a separate later step and must not hold
    up the spend number."""
    snaps, _ = to_snapshots(
        [row()], platform="p", campaign_id="c", platform_campaign_id="pc",
        attribution_window="w", currency="USD", retrieved_at=T0,
    )
    assert snaps[0].creative_content_hash is None


# --------------------------------------------------------------------------------------
# append-only storage with read-time resolution
# --------------------------------------------------------------------------------------


def test_the_newest_source_timestamp_wins():
    store = SnapshotStore()
    store.append([snapshot(spend_minor=100, source_updated_at=T0)])
    store.append([snapshot(spend_minor=250, source_updated_at=T0 + 3600)])

    current = store.latest_effective()
    assert len(current) == 1
    assert current[0].spend_minor == 250
    # Nothing was destroyed: the record of what was known at decision time survives.
    assert len(store.all_rows()) == 2


def test_a_late_arriving_older_restatement_does_not_win():
    """THE invariant. Insertion order is not the tie breaker; the source timestamp is.

    Ad platforms restate history, so a correction for last Tuesday can arrive on Thursday.
    An "append wins" rule would let a stale replay silently overwrite a newer correction.
    """
    store = SnapshotStore()
    store.append([snapshot(spend_minor=250, source_updated_at=T0 + 3600)])
    store.append([snapshot(spend_minor=100, source_updated_at=T0)])  # older, appended LAST

    current = store.latest_effective()
    assert len(current) == 1
    assert current[0].spend_minor == 250
    assert current[0].source_updated_at == T0 + 3600


def test_a_tie_on_the_source_timestamp_falls_back_to_insertion_order():
    """Deterministic rather than arbitrary, so two runs over the same data agree."""
    store = SnapshotStore()
    store.append([snapshot(spend_minor=100, source_updated_at=T0)])
    store.append([snapshot(spend_minor=200, source_updated_at=T0)])
    assert store.latest_effective()[0].spend_minor == 200


def test_readings_for_different_ads_days_or_windows_are_all_current_at_once():
    store = SnapshotStore()
    store.append(
        [
            snapshot(platform_ad_id="ad-1"),
            snapshot(platform_ad_id="ad-2"),
            snapshot(metric_date="2026-01-02"),
            snapshot(attribution_window="1d_click"),
        ]
    )
    assert len(store.latest_effective()) == 4


def test_append_returns_the_number_of_rows_written():
    store = SnapshotStore()
    assert store.append([snapshot(), snapshot(platform_ad_id="ad-2")]) == 2
    assert store.append([]) == 0


def test_all_rows_returns_a_copy_that_cannot_mutate_the_store():
    store = SnapshotStore()
    store.append([snapshot()])
    store.all_rows().clear()
    assert len(store.all_rows()) == 1


# --------------------------------------------------------------------------------------
# total spend
# --------------------------------------------------------------------------------------


def test_total_spend_is_none_when_every_reading_is_unknown():
    """A budget guard reading "0 spent" from "we cannot see the spend" would let a
    campaign run forever."""
    store = SnapshotStore()
    store.append(
        [snapshot(spend_minor=None), snapshot(platform_ad_id="ad-2", spend_minor=None)]
    )
    assert store.total_spend_minor() is None


def test_total_spend_is_none_for_an_empty_store():
    assert SnapshotStore().total_spend_minor() is None


def test_total_spend_is_zero_when_a_zero_was_genuinely_reported():
    """The distinction the None case exists to preserve, stated from the other side."""
    store = SnapshotStore()
    store.append([snapshot(spend_minor=0)])
    assert store.total_spend_minor() == 0


def test_total_spend_sums_only_the_current_readings():
    store = SnapshotStore()
    store.append([snapshot(platform_ad_id="ad-1", spend_minor=100, source_updated_at=T0)])
    store.append([snapshot(platform_ad_id="ad-1", spend_minor=250, source_updated_at=T0 + 60)])
    store.append([snapshot(platform_ad_id="ad-2", spend_minor=50, source_updated_at=T0)])
    assert store.total_spend_minor() == 300


def test_total_spend_ignores_unknown_readings_without_treating_them_as_zero():
    store = SnapshotStore()
    store.append(
        [
            snapshot(platform_ad_id="ad-1", spend_minor=100),
            snapshot(platform_ad_id="ad-2", spend_minor=None),
        ]
    )
    assert store.total_spend_minor() == 100


# --------------------------------------------------------------------------------------
# the collector against the dry-run platform
# --------------------------------------------------------------------------------------


@pytest.fixture
def platform_with_campaign():
    platform = DryRunPlatform(account_confirmed=True)
    state = platform.create_paused(
        intent_digest="a" * 32,
        lifetime_budget_minor=25_000,
        currency="USD",
        starts_at="2026-03-01T09:00:00+00:00",
        ends_at="2026-03-08T09:00:00+00:00",
    )
    return platform, state


def test_a_collection_pass_appends_rows_and_reports_row_errors(platform_with_campaign):
    platform, state = platform_with_campaign
    store = SnapshotStore()
    written, errors = Collector(platform=platform, store=store, trailing_days=4).collect(
        campaign_id="camp-1",
        platform_campaign_id=state.platform_campaign_id,
        currency="USD",
        now=T0,
    )
    assert written == 4
    assert errors == []
    assert len(store.all_rows()) == 4


def test_every_collection_request_is_a_get(platform_with_campaign):
    """Reporting is outside the money path by construction, not by convention."""
    platform, state = platform_with_campaign
    platform.requests.clear()
    Collector(platform=platform, store=SnapshotStore(), trailing_days=3).collect(
        campaign_id="camp-1",
        platform_campaign_id=state.platform_campaign_id,
        currency="USD",
        now=T0,
    )
    assert platform.requests
    assert {method for method, _, _ in platform.requests} == {"GET"}


def test_re_collecting_the_trailing_window_appends_without_double_counting(
    platform_with_campaign,
):
    """The window is re-fetched rather than only the new day, because that is how a late
    restatement of an earlier day is ever seen. Double counting is prevented structurally
    by `latest_effective`, not by a write-time "have I seen this day" check, which would
    discard the correction the trailing window exists to catch.
    """
    platform, state = platform_with_campaign
    store = SnapshotStore()
    collector = Collector(platform=platform, store=store, trailing_days=5)
    args = dict(
        campaign_id="camp-1",
        platform_campaign_id=state.platform_campaign_id,
        currency="USD",
    )

    collector.collect(now=T0, **args)
    collector.collect(now=T0 + 3600, **args)

    assert len(store.all_rows()) == 10
    assert len(store.latest_effective()) == 5
    # And the current view is the SECOND collection's rows.
    assert {r.source_updated_at for r in store.latest_effective()} == {T0 + 3600}


def test_purchases_stay_unknown_on_the_rows_where_the_platform_reported_no_actions(
    platform_with_campaign,
):
    """The dry-run platform deliberately omits actions on some rows so the collector's
    None-not-zero behavior is exercised end to end and not only in a unit."""
    platform, state = platform_with_campaign
    store = SnapshotStore()
    Collector(platform=platform, store=store, trailing_days=6).collect(
        campaign_id="camp-1",
        platform_campaign_id=state.platform_campaign_id,
        currency="USD",
        now=T0,
    )
    purchases = [r.purchases for r in store.latest_effective()]
    assert None in purchases
    assert any(p is not None for p in purchases)


def test_the_collector_does_not_loop(platform_with_campaign):
    """Cadence belongs to a scheduler. A process that loops forever looks identical
    whether it is working or wedged."""
    platform, state = platform_with_campaign
    platform.requests.clear()
    Collector(platform=platform, store=SnapshotStore(), trailing_days=3).collect(
        campaign_id="camp-1",
        platform_campaign_id=state.platform_campaign_id,
        currency="USD",
        now=T0,
    )
    insight_calls = [r for r in platform.requests if "insights" in r[1]]
    assert len(insight_calls) == 1
