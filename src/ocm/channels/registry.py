"""Config-driven channel construction.

Adding a channel is a config edit plus one adapter class. The orchestrator is not touched,
which is the test of whether the abstraction is real.

`kind` in config selects the adapter class; everything else in the table is that adapter's
own settings. An unknown `kind` is a hard error at load time rather than a silent skip: a
channel that quietly fails to register is a channel that quietly stops publishing, and
nobody notices for a month.
"""

from __future__ import annotations

from typing import Any, Callable

from .adapters import LongFormAdapter, ShortFormAdapter

ADAPTER_KINDS: dict[str, Callable[[dict, Any], Any]] = {
    "long_form": LongFormAdapter.from_config,
    "short_form": ShortFormAdapter.from_config,
}


def build_channels(cfg: dict, transport: Any) -> dict[str, Any]:
    """Build {channel_name: adapter} from the `[channels]` section of a config."""
    channels: dict[str, Any] = {}
    entries = cfg.get("channels", [])
    if not entries:
        raise ValueError("config defines no channels")
    for entry in entries:
        kind = entry.get("kind")
        if kind not in ADAPTER_KINDS:
            raise ValueError(
                f"unknown channel kind {kind!r}; known kinds: {sorted(ADAPTER_KINDS)}"
            )
        if not entry.get("enabled", True):
            continue
        adapter = ADAPTER_KINDS[kind](entry, transport)
        if adapter.name in channels:
            raise ValueError(f"duplicate channel name {adapter.name!r}")
        channels[adapter.name] = adapter
    if not channels:
        raise ValueError("all configured channels are disabled")
    return channels
