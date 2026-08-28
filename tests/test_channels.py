"""Channel adapters, transports, and the config-driven registry.

The load-bearing idea: `normalize` lives on the ADAPTER, because only the channel knows
that its "view" is cheap and its "reply" is expensive. Ranking a newsletter against a tweet
by raw engagement count compares nothing.
"""

from __future__ import annotations

import pytest
from conftest import FIXED_NOW, make_draft

from ocm.channels.adapters import LongFormAdapter, ShortFormAdapter
from ocm.channels.base import ChannelRequest, ChannelResponse
from ocm.channels.registry import ADAPTER_KINDS, build_channels
from ocm.channels.transport import DryRunTransport, LiveTransport
from ocm.models import EngagementRecord

LONG_BODY = "x" * 500
SHORT_BODY = "a short public-feed post"


def engagement(channel: str, **metrics) -> EngagementRecord:
    return EngagementRecord(
        publish_id="pub-1", channel=channel, collected_at=FIXED_NOW, metrics=metrics
    )


@pytest.fixture
def transport() -> DryRunTransport:
    return DryRunTransport()


@pytest.fixture
def long_form(transport) -> LongFormAdapter:
    return LongFormAdapter(
        name="substack",
        transport=transport,
        min_body_chars=200,
        max_body_chars=60_000,
        require_title=True,
        engagement_saturation=100.0,
    )


@pytest.fixture
def short_form(transport) -> ShortFormAdapter:
    return ShortFormAdapter(
        name="x",
        transport=transport,
        max_body_chars=280,
        min_impressions_for_signal=100,
        rate_saturation=0.05,
    )


def fields(errors) -> set[str]:
    return {e.field for e in errors}


# --------------------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------------------


def test_long_form_requires_a_title(long_form):
    assert fields(long_form.validate(make_draft(LONG_BODY, title=""))) == {"title"}
    assert fields(long_form.validate(make_draft(LONG_BODY, title="   "))) == {"title"}
    assert long_form.validate(make_draft(LONG_BODY, title="A headline")) == []


def test_long_form_enforces_a_minimum_and_a_maximum_body(long_form):
    assert fields(long_form.validate(make_draft("x" * 199, title="t"))) == {"body"}
    assert long_form.validate(make_draft("x" * 200, title="t")) == []
    assert fields(long_form.validate(make_draft("x" * 60_001, title="t"))) == {"body"}


def test_long_form_can_be_configured_not_to_require_a_title(transport):
    adapter = LongFormAdapter(transport=transport, require_title=False, min_body_chars=0)
    assert adapter.validate(make_draft("body", title="")) == []


def test_short_form_rejects_a_title(short_form):
    """A title on a channel that does not carry one is content the reader never sees."""
    assert fields(short_form.validate(make_draft(SHORT_BODY, title="A headline"))) == {"title"}
    assert short_form.validate(make_draft(SHORT_BODY)) == []


def test_short_form_enforces_the_character_cap(short_form):
    assert short_form.validate(make_draft("x" * 280)) == []
    assert fields(short_form.validate(make_draft("x" * 281))) == {"body"}


def test_short_form_rejects_an_empty_body(short_form):
    assert fields(short_form.validate(make_draft("   "))) == {"body"}


def test_validation_reports_every_problem_at_once(short_form):
    errors = short_form.validate(make_draft("x" * 400, title="A headline"))
    assert fields(errors) == {"body", "title"}


def test_validation_measures_the_stripped_body(long_form):
    assert fields(long_form.validate(make_draft("  " + "x" * 199 + "  ", title="t"))) == {"body"}


# --------------------------------------------------------------------------------------
# normalization: the reason it lives on the adapter
# --------------------------------------------------------------------------------------


def test_long_form_scores_absolute_engagement(long_form):
    """A newsletter is read by people who already subscribed, so reach is roughly fixed
    and a post with twice the replies really was twice as good."""
    assert long_form.normalize(engagement("substack", engagements=50)) == pytest.approx(0.5)
    assert long_form.normalize(engagement("substack", engagements=0)) == 0.0


def test_long_form_saturates_at_one(long_form):
    assert long_form.normalize(engagement("substack", engagements=100)) == 1.0
    assert long_form.normalize(engagement("substack", engagements=100_000)) == 1.0


def test_long_form_ignores_impressions_entirely(long_form):
    a = long_form.normalize(engagement("substack", engagements=50, impressions=100))
    b = long_form.normalize(engagement("substack", engagements=50, impressions=1_000_000))
    assert a == b


def test_short_form_scores_the_engagement_rate(short_form):
    """A public feed shows a post to a variable and largely unpredictable number of people,
    so absolute engagement mostly measures how the ranking system felt that hour."""
    assert short_form.normalize(
        engagement("x", impressions=1000, engagements=25)
    ) == pytest.approx(0.5)  # 2.5% rate against a 5% saturation


def test_short_form_saturates_at_one(short_form):
    assert short_form.normalize(engagement("x", impressions=1000, engagements=500)) == 1.0


def test_short_form_returns_zero_below_the_impression_floor_even_at_a_great_rate(short_form):
    """Three engagements on eleven impressions is a 27 percent rate and means nothing.
    Returning 0.0 rather than a flattering ratio keeps under-delivered posts from being
    learned from as if they were wins.
    """
    flattering = engagement("x", impressions=11, engagements=3)
    assert flattering.metrics["engagements"] / flattering.metrics["impressions"] > 0.25
    assert short_form.normalize(flattering) == 0.0


def test_short_form_scores_normally_exactly_at_the_impression_floor(short_form):
    assert short_form.normalize(engagement("x", impressions=100, engagements=5)) == 1.0
    assert short_form.normalize(engagement("x", impressions=99, engagements=5)) == 0.0


def test_missing_metrics_score_as_zero_rather_than_raising(long_form, short_form):
    assert long_form.normalize(engagement("substack")) == 0.0
    assert short_form.normalize(engagement("x")) == 0.0


@pytest.mark.parametrize("saturation", [0.0, -1.0])
def test_a_non_positive_saturation_scores_zero_rather_than_dividing_by_zero(
    transport, saturation
):
    long_adapter = LongFormAdapter(transport=transport, engagement_saturation=saturation)
    short_adapter = ShortFormAdapter(transport=transport, rate_saturation=saturation)
    assert long_adapter.normalize(engagement("substack", engagements=50)) == 0.0
    assert short_adapter.normalize(engagement("x", impressions=1000, engagements=50)) == 0.0


def test_the_two_adapters_rank_the_same_pair_of_posts_in_OPPOSITE_orders(long_form, short_form):
    """THE point of per-channel normalization.

    `newsletter_like` has high ABSOLUTE engagement and a terrible rate (it reached a huge
    audience). `feed_like` has trivial absolute engagement and an excellent rate. On one
    shared leaderboard by raw counts, the newsletter wins forever and the loop learns the
    wrong lesson. Each adapter scores in its OWN terms, so each ranks by its own metric.
    """
    newsletter_like = engagement("substack", engagements=80, impressions=100_000)
    feed_like = engagement("x", engagements=10, impressions=200)

    assert long_form.normalize(newsletter_like) > long_form.normalize(feed_like)
    assert short_form.normalize(feed_like) > short_form.normalize(newsletter_like)

    # Stated as the disagreement itself, so the test fails if either adapter is changed to
    # use the other's metric.
    long_order = [long_form.normalize(newsletter_like), long_form.normalize(feed_like)]
    short_order = [short_form.normalize(newsletter_like), short_form.normalize(feed_like)]
    assert (long_order[0] > long_order[1]) != (short_order[0] > short_order[1])


def test_every_normalized_score_is_inside_the_unit_interval(long_form, short_form):
    cases = [
        engagement("substack", engagements=-5),
        engagement("substack", engagements=10**9),
        engagement("x", impressions=1000, engagements=-5),
        engagement("x", impressions=1000, engagements=10**9),
    ]
    for record in cases:
        assert 0.0 <= long_form.normalize(record) <= 1.0
        assert 0.0 <= short_form.normalize(record) <= 1.0


# --------------------------------------------------------------------------------------
# publish and collect go through the transport
# --------------------------------------------------------------------------------------


def test_publishing_goes_through_the_transport_and_returns_proof(long_form, transport):
    record = long_form.publish(make_draft(LONG_BODY, title="A headline"))

    assert len(transport.requests_for("publish")) == 1
    assert transport.requests_for("publish")[0].payload["title"] == "A headline"
    assert record.channel == "substack"
    assert record.external_id.startswith("dryrun-")
    assert record.dry_run is True


def test_the_short_form_publish_payload_carries_no_title(short_form, transport):
    short_form.publish(make_draft(SHORT_BODY))
    assert "title" not in transport.requests_for("publish")[0].payload


def test_a_failed_transport_response_raises_rather_than_returning_a_record(long_form):
    class RefusingTransport:
        is_dry_run = True

        def send(self, request):
            return ChannelResponse(ok=False, error="the platform said no")

    long_form.transport = RefusingTransport()
    with pytest.raises(RuntimeError, match="the platform said no"):
        long_form.publish(make_draft(LONG_BODY, title="t"))


def test_collect_returns_the_metrics_the_transport_reported(long_form, transport):
    transport.synthetic_metrics["substack"] = {"engagements": 42.0}
    record = long_form.publish(make_draft(LONG_BODY, title="t"))
    assert long_form.collect(record).metrics == {"engagements": 42.0}


def test_collect_survives_a_response_with_no_data(short_form):
    class SilentTransport:
        is_dry_run = True

        def send(self, request):
            return ChannelResponse(ok=True, external_id="e", data=None, dry_run=True)

    short_form.transport = SilentTransport()
    record = short_form.publish(make_draft(SHORT_BODY))
    assert short_form.collect(record).metrics == {}


# --------------------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------------------


def test_the_dry_run_transport_declares_itself_and_records_every_request(transport):
    assert transport.is_dry_run is True
    transport.send(ChannelRequest("substack", "publish", {"body": "b"}))
    transport.send(ChannelRequest("substack", "collect", {"external_id": "e"}))
    assert len(transport.requests) == 2
    assert [r.operation for r in transport.requests] == ["publish", "collect"]
    assert len(transport.requests_for("publish")) == 1


def test_the_dry_run_transport_is_deterministic(transport):
    """So the end-to-end demo produces the same output every run and a test can assert on
    exact values instead of on shapes."""
    request = ChannelRequest("substack", "publish", {"body": "b"})
    a = transport.send(request)
    b = DryRunTransport().send(request)
    assert a.external_id == b.external_id
    assert a.external_url == b.external_url


def test_synthetic_ids_are_visibly_synthetic(transport):
    """An id that could be mistaken for a real one is a trap for whoever reads the log."""
    response = transport.send(ChannelRequest("substack", "publish", {"body": "b"}))
    assert response.external_id.startswith("dryrun-")
    assert "example.invalid" in response.external_url


def test_synthetic_collect_metrics_are_stable_per_post(transport):
    request = ChannelRequest("x", "collect", {"external_id": "ext-1"})
    assert transport.send(request).data == transport.send(request).data
    other = transport.send(ChannelRequest("x", "collect", {"external_id": "ext-2"}))
    assert other.data != transport.send(request).data


def test_an_unknown_operation_is_refused_rather_than_silently_succeeding(transport):
    response = transport.send(ChannelRequest("x", "delete", {}))
    assert response.ok is False
    assert "unknown operation" in response.error


def test_the_live_transport_declares_itself_not_a_dry_run_and_refuses_to_send():
    """A portfolio repository that shipped a half-written HTTP client for someone else's
    private API would be a maintenance liability that never gets exercised. The shape shows
    where a real client plugs in; the body refuses.
    """
    live = LiveTransport()
    assert live.is_dry_run is False
    with pytest.raises(NotImplementedError, match="ships no network client"):
        live.send(ChannelRequest("substack", "publish", {"body": "b"}))


# --------------------------------------------------------------------------------------
# the registry
# --------------------------------------------------------------------------------------


def channel_cfg(**overrides) -> dict:
    entry = {"kind": "long_form", "name": "substack", "enabled": True}
    entry.update(overrides)
    return entry


def test_the_registry_builds_the_configured_channels(transport):
    channels = build_channels(
        {"channels": [channel_cfg(), channel_cfg(kind="short_form", name="x")]}, transport
    )
    assert set(channels) == {"substack", "x"}
    assert isinstance(channels["substack"], LongFormAdapter)
    assert isinstance(channels["x"], ShortFormAdapter)
    assert channels["x"].transport is transport


def test_an_unknown_kind_is_a_hard_error_at_load_time(transport):
    """A channel that quietly fails to register is a channel that quietly stops
    publishing, and nobody notices for a month."""
    with pytest.raises(ValueError, match="unknown channel kind"):
        build_channels({"channels": [channel_cfg(kind="carrier_pigeon")]}, transport)


def test_the_unknown_kind_error_names_the_kinds_that_do_exist(transport):
    with pytest.raises(ValueError) as exc:
        build_channels({"channels": [channel_cfg(kind="nope")]}, transport)
    for kind in ADAPTER_KINDS:
        assert kind in str(exc.value)


def test_a_disabled_channel_is_skipped(transport):
    channels = build_channels(
        {
            "channels": [
                channel_cfg(),
                channel_cfg(kind="short_form", name="x", enabled=False),
            ]
        },
        transport,
    )
    assert set(channels) == {"substack"}


def test_a_channel_with_no_enabled_key_defaults_to_enabled(transport):
    entry = channel_cfg()
    del entry["enabled"]
    assert set(build_channels({"channels": [entry]}, transport)) == {"substack"}


def test_a_disabled_channel_with_an_unknown_kind_still_raises(transport):
    """The kind check runs first on purpose: a typo'd kind sitting behind `enabled=false`
    is a landmine that goes off the day someone flips the flag."""
    with pytest.raises(ValueError, match="unknown channel kind"):
        build_channels({"channels": [channel_cfg(kind="typo", enabled=False)]}, transport)


def test_a_duplicate_channel_name_raises(transport):
    """Two entries under one name means one silently shadows the other, so one configured
    channel simply never publishes."""
    with pytest.raises(ValueError, match="duplicate channel name"):
        build_channels(
            {"channels": [channel_cfg(), channel_cfg(kind="short_form", name="substack")]},
            transport,
        )


def test_no_channels_at_all_raises(transport):
    with pytest.raises(ValueError, match="no channels"):
        build_channels({"channels": []}, transport)
    with pytest.raises(ValueError, match="no channels"):
        build_channels({}, transport)


def test_every_channel_disabled_raises(transport):
    """Silently building an empty channel map would make the loop run happily forever
    while publishing nothing."""
    with pytest.raises(ValueError, match="all configured channels are disabled"):
        build_channels(
            {
                "channels": [
                    channel_cfg(enabled=False),
                    channel_cfg(kind="short_form", name="x", enabled=False),
                ]
            },
            transport,
        )


def test_the_shipped_organic_config_builds_both_example_channels(transport):
    from conftest import CONFIG_DIR

    from ocm import config as cfgmod

    conf = cfgmod.load(CONFIG_DIR / "organic.toml")
    channels = build_channels(conf.data, transport)
    assert set(channels) == {"substack", "x"}
