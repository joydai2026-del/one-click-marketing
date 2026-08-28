"""The style space: the thing the loop actually learns over.

An always-on content loop needs an answer to "learn WHAT, exactly?". Learning over raw
text is hopeless, because no two posts are comparable. So content is generated inside a
small, explicit, typed style space, and the loop learns which COORDINATES win.

The example space has three axes (hook, angle, format), each with a handful of values.
Every draft is tagged with its coordinates, every engagement measurement attaches to those
tags, and the ranker can then say something falsifiable: "on this channel, this hook
averaged higher than that one across N samples". You cannot get that sentence out of a
pile of free text.

Two mechanisms here are worth more than the rest.

ROTATION IS DETERMINISTIC.
`rotation_at(offset, n)` advances each axis on its OWN modulus. Given the same offset it
returns the same styles forever. That determinism is what makes the whole pipeline
idempotent under retry: the same tick re-run produces the same style, which produces the
same variant id, which the store already has, so a crashed-and-restarted run stages nothing
new instead of double-posting.

EXPLORATION POSITIONS ARE DERIVED, NEVER HAND-PICKED.
A loop that always plays the current winner stops learning immediately: it never gathers
evidence about anything else, so its first accidental winner is its winner forever. The fix
is to reserve some positions for pure exploration, where the winners are ignored and the
plain rotation is used.

The trap is picking those positions by hand, or by a round number like "every third tick".
If an axis happens to have 3 values, "every third tick" resonates with it and explores the
SAME value of that axis every single time, so two of its three values are never explored at
all. Nobody notices, because the loop still looks busy.

`exploration_positions` therefore derives the schedule from the axis lengths: the cycle is
the lowest common multiple of them, and the stride is the smallest step coprime with that
cycle. Coprimality is what guarantees the stride walks the entire cycle before repeating,
so every value of every axis gets explored. Add a value to an axis and the schedule
recomputes itself. There is a property test for exactly this.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import gcd, lcm


@dataclass(frozen=True)
class Axis:
    """One dimension of the style space, for example "hook"."""

    name: str
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("axis name must not be empty")
        if len(self.values) == 0:
            raise ValueError(f"axis {self.name!r} has no values")
        if len(set(self.values)) != len(self.values):
            raise ValueError(f"axis {self.name!r} has duplicate values")


@dataclass(frozen=True)
class Style:
    """One point in the style space. Frozen: a style must not drift after an id is
    derived from it, or the tags recorded against a measurement would describe content
    that was never generated."""

    coords: tuple[tuple[str, str], ...]  # ((axis_name, value), ...) in axis order

    @property
    def style_id(self) -> str:
        return ".".join(v for _, v in self.coords)

    def tags(self) -> dict[str, str]:
        return dict(self.coords)

    def value(self, axis_name: str) -> str:
        for name, val in self.coords:
            if name == axis_name:
                return val
        raise KeyError(axis_name)


@dataclass(frozen=True)
class StyleSpace:
    axes: tuple[Axis, ...]

    @classmethod
    def from_config(cls, cfg: dict) -> StyleSpace:
        axes = tuple(
            Axis(name=a["name"], values=tuple(a["values"])) for a in cfg.get("axes", [])
        )
        if not axes:
            raise ValueError("style space defines no axes")
        if len({a.name for a in axes}) != len(axes):
            raise ValueError("style space has duplicate axis names")
        return cls(axes=axes)

    @property
    def dimensions(self) -> tuple[str, ...]:
        return tuple(a.name for a in self.axes)

    @property
    def size(self) -> int:
        n = 1
        for a in self.axes:
            n *= len(a.values)
        return n

    def rotation_at(self, offset: int, n: int) -> list[Style]:
        """`n` styles starting at `offset`, each axis advancing on its own modulus.

        Advancing per-axis rather than enumerating the full cross product means a short
        run still varies every axis, instead of varying only the last one.
        """
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if n < 0:
            raise ValueError("n must be non-negative")
        out: list[Style] = []
        for i in range(n):
            pos = offset + i
            coords = tuple(
                (axis.name, axis.values[pos % len(axis.values)]) for axis in self.axes
            )
            out.append(Style(coords=coords))
        return out

    def style_from_id(self, style_id: str) -> Style:
        """Fail-closed inverse of `style_id`, for rehydrating a persisted plan."""
        parts = style_id.split(".")
        if len(parts) != len(self.axes):
            raise ValueError(f"style id {style_id!r} does not match this space")
        coords: list[tuple[str, str]] = []
        for axis, part in zip(self.axes, parts, strict=True):
            if part not in axis.values:
                raise ValueError(f"{part!r} is not a value of axis {axis.name!r}")
            coords.append((axis.name, part))
        return Style(coords=tuple(coords))

    def exploration_positions(self, fraction: float = 0.34) -> tuple[int, frozenset[int]]:
        """Return (cycle_length, positions) reserved for pure exploration.

        `fraction` is the share of positions to reserve, floored at the longest axis so
        that even a tiny fraction still gives every value of the widest axis a chance.
        """
        lengths = [len(a.values) for a in self.axes]
        cycle = lcm(*lengths) if len(lengths) > 1 else lengths[0]
        want = max(int(cycle * fraction), max(lengths))
        want = min(want, cycle)
        stride = _smallest_coprime_stride(cycle)
        positions = {(i * stride) % cycle for i in range(want)}
        return cycle, frozenset(positions)

    def is_exploration(self, position: int, fraction: float = 0.34) -> bool:
        cycle, positions = self.exploration_positions(fraction)
        return (position % cycle) in positions


def _smallest_coprime_stride(cycle: int) -> int:
    """Smallest stride > 0 that is coprime with `cycle`.

    Coprimality is the whole point: a stride sharing a factor with the cycle visits only
    a subset of positions no matter how many steps you take, which is precisely the
    starvation bug this module exists to avoid.
    """
    if cycle <= 1:
        return 1
    for s in range(1, cycle):
        if gcd(s, cycle) == 1:
            return s
    return 1  # pragma: no cover - unreachable for cycle > 1
