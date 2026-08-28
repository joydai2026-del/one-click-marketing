"""Configuration loading. TOML or JSON, stdlib only, no secrets.

CONFIG-NOT-CODE, STATED PRECISELY

Any value an operator might reasonably want to change without a deploy lives here: the
rubric and its thresholds, the compliance term list and structural rules, the style space,
the channel list and each channel's limits, the learning parameters, the schedule policy,
and every paid guardrail. None of those has a meaningful hardcoded default in the engine.

WHAT NEVER LIVES HERE

Credentials. A config file may only reference an environment variable, using the exact
form `${UPPER_SNAKE}`. A literal value in a credential field is REFUSED, not accepted with
a warning, because a warning gets scrolled past and a secret in a config file gets
committed. `strict_env=False` skips resolution entirely for preview and dry-run paths, so
the demo needs no environment at all.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ENV_REF = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")


class ConfigError(Exception):
    pass


def load_raw(path: str | Path) -> dict[str, Any]:
    """Load a .toml or .json config file into a plain dict."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".toml":
        return tomllib.loads(text)
    if p.suffix == ".json":
        return json.loads(text)
    raise ConfigError(f"unsupported config format {p.suffix!r}; use .toml or .json")


def resolve_env_ref(value: str, *, strict: bool) -> str:
    """Resolve `${VAR}` from the environment. Refuse a literal.

    Refusing rather than accepting is the entire control. Once a literal is permitted
    "just this once", the config file becomes a place secrets can live.
    """
    m = _ENV_REF.match(value.strip())
    if m is None:
        raise ConfigError(
            "credential fields must be an environment reference of the form ${VAR_NAME}; "
            "a literal value is refused so that secrets cannot live in config"
        )
    if not strict:
        return ""
    name = m.group(1)
    resolved = os.environ.get(name)
    if not resolved:
        raise ConfigError(f"environment variable {name} is referenced by config but not set")
    return resolved


@dataclass(frozen=True)
class LoadedConfig:
    """A parsed config plus the absolute directory it came from.

    `source_dir` is threaded onward so relative references resolve against the CONFIG,
    never against whatever directory the process happens to be started in.
    """

    data: dict[str, Any]
    source_dir: str
    path: str

    def section(self, name: str) -> dict[str, Any]:
        value = self.data.get(name)
        if value is None:
            raise ConfigError(f"config {self.path} has no [{name}] section")
        if not isinstance(value, dict):
            raise ConfigError(f"config section [{name}] must be a table")
        return value

    def optional(self, name: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        value = self.data.get(name)
        if value is None:
            return dict(default or {})
        if not isinstance(value, dict):
            raise ConfigError(f"config section [{name}] must be a table")
        return value


def load(path: str | Path) -> LoadedConfig:
    p = Path(path).resolve()
    return LoadedConfig(data=load_raw(p), source_dir=str(p.parent), path=str(p))
