"""The style space: deterministic rotation, id round-tripping, and anti-starvation.

The property test at the bottom is the one that matters. Everything else is scaffolding
that makes it meaningful.
"""

from __future__ import annotations

import dataclasses
from math import gcd, lcm

import pytest

from ocm.generation.style import (
    Axis,
    Style,
    StyleSpace,
    _smallest_coprime_stride,
)


def space_of(*lengths: int) -> StyleSpace:
    """A space whose axis values are globally unique, so coverage is unambiguous."""
    return StyleSpace(
        axes=tuple(
            Axis(name=f"ax{i}", values=tuple(f"ax{i}v{j}" for j in range(n)))
            for i, n in enumerate(lengths)
        )
    )


# --------------------------------------------------------------------------------------
# axes
# --------------------------------------------------------------------------------------


def test_axis_rejects_an_empty_value_list():
    """An axis with nothing in it would make `values[pos % 0]` a ZeroDivisionError later."""
    with pytest.raises(ValueError, match="no values"):
        Axis(name="hook", values=())


def test_axis_rejects_duplicate_values():
    """A duplicated value would get two chances at every draw, silently biasing the space
    and splitting one value's measurements across two identical-looking groups."""
    with pytest.raises(ValueError, match="duplicate"):
        Axis(name="hook", values=("a", "b", "a"))


def test_axis_rejects_an_empty_name():
    with pytest.raises(ValueError, match="name"):
        Axis(name="", values=("a",))


def test_style_space_rejects_no_axes_and_duplicate_axis_names():
    with pytest.raises(ValueError, match="no axes"):
        StyleSpace.from_config({"axes": []})
    with pytest.raises(ValueError, match="duplicate axis names"):
        StyleSpace.from_config(
            {"axes": [{"name": "h", "values": ["a"]}, {"name": "h", "values": ["b"]}]}
        )


def test_size_is_the_cross_product_and_dimensions_are_the_axis_names():
    space = space_of(3, 5, 2)
    assert space.size == 30
    assert space.dimensions == ("ax0", "ax1", "ax2")


# --------------------------------------------------------------------------------------
# rotation
# --------------------------------------------------------------------------------------


def test_rotation_at_is_deterministic_across_calls():
    """Determinism is what makes the pipeline idempotent under retry: same tick, same
    style, same variant id, so a crashed-and-restarted run stages nothing new."""
    space = space_of(3, 4)
    assert space.rotation_at(7, 5) == space.rotation_at(7, 5)


def test_each_axis_advances_on_its_own_modulus():
    """Enumerating the cross product instead would vary only the last axis in a short run."""
    space = space_of(2, 3)
    ids = [s.style_id for s in space.rotation_at(0, 6)]
    assert ids == [
        "ax0v0.ax1v0",
        "ax0v1.ax1v1",
        "ax0v0.ax1v2",
        "ax0v1.ax1v0",
        "ax0v0.ax1v1",
        "ax0v1.ax1v2",
    ]


def test_rotation_at_offset_matches_the_same_absolute_positions():
    """Offsetting is a window onto one infinite sequence, not a separate sequence."""
    space = space_of(3, 4)
    whole = space.rotation_at(0, 12)
    assert space.rotation_at(5, 4) == whole[5:9]


@pytest.mark.parametrize("offset,n", [(-1, 1), (-5, 3)])
def test_rotation_at_refuses_a_negative_offset(offset, n):
    with pytest.raises(ValueError, match="offset"):
        space_of(2, 2).rotation_at(offset, n)


def test_rotation_at_refuses_a_negative_count():
    with pytest.raises(ValueError, match="n must be"):
        space_of(2, 2).rotation_at(0, -1)


def test_rotation_at_zero_returns_nothing_rather_than_raising():
    assert space_of(2, 2).rotation_at(0, 0) == []


# --------------------------------------------------------------------------------------
# style ids
# --------------------------------------------------------------------------------------


def test_style_id_round_trips_through_style_from_id():
    space = space_of(3, 4, 2)
    for style in space.rotation_at(0, 12):
        assert space.style_from_id(style.style_id) == style


def test_style_from_id_rejects_the_wrong_arity():
    """Fail-closed: a persisted plan from a space with different axes must not rehydrate."""
    space = space_of(3, 4)
    with pytest.raises(ValueError, match="does not match this space"):
        space.style_from_id("ax0v0")
    with pytest.raises(ValueError, match="does not match this space"):
        space.style_from_id("ax0v0.ax1v0.extra")


def test_style_from_id_rejects_a_value_the_axis_does_not_have():
    """Otherwise a stale id injects an unknown coordinate into a live plan."""
    space = space_of(3, 4)
    with pytest.raises(ValueError, match="is not a value of axis"):
        space.style_from_id("ax0v0.nonsense")


def test_style_from_id_rejects_values_that_belong_to_the_wrong_axis():
    """Positional, not set-membership: the value must be valid for THAT axis."""
    space = space_of(3, 4)
    with pytest.raises(ValueError):
        space.style_from_id("ax1v0.ax1v1")


def test_style_tags_and_value_expose_the_coordinates():
    style = space_of(2, 2).rotation_at(1, 1)[0]
    assert style.tags() == {"ax0": "ax0v1", "ax1": "ax1v1"}
    assert style.value("ax1") == "ax1v1"
    with pytest.raises(KeyError):
        style.value("no-such-axis")


def test_style_is_frozen():
    """A style that drifted after an id was derived from it would make every measurement
    tagged against it describe content that was never generated."""
    style = Style(coords=(("a", "1"),))
    with pytest.raises(dataclasses.FrozenInstanceError):
        style.coords = (("a", "2"),)  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# exploration schedule: the anti-starvation guarantee
# --------------------------------------------------------------------------------------

# Combinations chosen to include the ones a naive fixed stride gets wrong. Axes of length
# 3 and 3 are the canonical trap: "explore every third tick" resonates with a 3-value axis
# and explores the SAME value forever, leaving two thirds of it untouched with no error.
STARVATION_CASES = [(3, 3), (2, 4), (3, 5, 3), (4, 4), (2, 2), (5, 5, 5), (4, 6), (3, 4, 5), (7,)]


@pytest.mark.parametrize("lengths", STARVATION_CASES, ids=lambda p: "x".join(map(str, p)))
def test_exploration_positions_cover_every_value_of_every_axis(lengths):
    """THE anti-starvation property.

    Walk only the reserved exploration positions through the rotation, collect the value
    each axis takes, and require that the union covers that axis completely. An axis value
    that is never explored is an option the loop can never learn anything about, and the
    loop looks perfectly busy the whole time it is not learning.
    """
    space = space_of(*lengths)
    cycle, positions = space.exploration_positions()

    covered: dict[str, set[str]] = {a.name: set() for a in space.axes}
    for pos in sorted(positions):
        style = space.rotation_at(pos, 1)[0]
        for axis_name, value in style.coords:
            covered[axis_name].add(value)

    for axis in space.axes:
        assert covered[axis.name] == set(axis.values), (
            f"axis {axis.name!r} starved: exploration positions {sorted(positions)} of "
            f"cycle {cycle} only ever reach {sorted(covered[axis.name])}"
        )


@pytest.mark.parametrize("lengths", STARVATION_CASES, ids=lambda p: "x".join(map(str, p)))
def test_exploration_positions_are_a_subset_of_the_cycle_and_the_cycle_is_the_lcm(lengths):
    space = space_of(*lengths)
    cycle, positions = space.exploration_positions()
    assert cycle == lcm(*lengths) if len(lengths) > 1 else cycle == lengths[0]
    assert positions
    assert all(0 <= p < cycle for p in positions)
    assert len(positions) <= cycle


@pytest.mark.parametrize("lengths", STARVATION_CASES, ids=lambda p: "x".join(map(str, p)))
def test_at_least_the_widest_axis_worth_of_positions_is_reserved(lengths):
    """The floor at `max(lengths)` is what makes the coverage property above achievable
    even when the requested fraction rounds down to almost nothing."""
    space = space_of(*lengths)
    _, positions = space.exploration_positions(fraction=0.0)
    assert len(positions) >= max(lengths)


def test_adding_a_value_to_an_axis_recomputes_the_schedule_with_no_other_change():
    """The schedule is DERIVED. A hand-picked list would silently starve the new value."""
    before_cycle, before = space_of(3, 4).exploration_positions()
    after_cycle, after = space_of(5, 4).exploration_positions()
    assert (before_cycle, before) != (after_cycle, after)


def test_is_exploration_agrees_with_exploration_positions_and_wraps_by_cycle():
    space = space_of(3, 4)
    cycle, positions = space.exploration_positions()
    for pos in range(cycle * 3):
        assert space.is_exploration(pos) is (pos % cycle in positions)


@pytest.mark.parametrize("cycle", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 30, 60, 210])
def test_smallest_coprime_stride_is_coprime_with_the_cycle(cycle):
    """Coprimality is the property the whole schedule rests on: a stride sharing a factor
    with the cycle visits only a subset of positions no matter how many steps it takes."""
    stride = _smallest_coprime_stride(cycle)
    assert stride > 0
    assert gcd(stride, cycle) == 1


@pytest.mark.parametrize("cycle", [2, 3, 4, 6, 10, 12])
def test_the_stride_walks_the_entire_cycle_before_repeating(cycle):
    """The consequence of coprimality, stated directly rather than assumed."""
    stride = _smallest_coprime_stride(cycle)
    assert {(i * stride) % cycle for i in range(cycle)} == set(range(cycle))
