"""THE HONESTY TESTS.

The README makes claims about this repository: no network client ships here, no credential
ships here, nothing can publish, nothing can spend. A claim in prose is a claim. These are
the mechanical checks that back each one, so the claims cannot quietly stop being true.
"""

from __future__ import annotations

import ast
import re

import pytest
from conftest import CONFIG_DIR, FIXED_NOW, REPO_ROOT, SRC_DIR

from ocm.cli import _build_organic
from ocm.paid.platform import CampaignState, DryRunPlatform, PlatformRefused

# Every stdlib module that could open a socket, plus the two ubiquitous third-party HTTP
# clients. `urllib.request` specifically, not all of `urllib`: `urllib.parse` is harmless.
NETWORK_MODULES = {
    "requests",
    "httpx",
    "aiohttp",
    "urllib.request",
    "urllib3",
    "socket",
    "ssl",
    "http.client",
    "ftplib",
    "smtplib",
    "telnetlib",
    "asyncio",
    "xmlrpc.client",
}


def python_sources() -> list:
    return sorted(SRC_DIR.rglob("*.py"))


def imported_modules(path) -> set[str]:
    """Every module name a file imports, via AST so a docstring cannot be a false positive.

    `channels/base.py` contains the sentence "An adapter never opens a socket", which a
    substring scan would flag. Parsing the syntax tree asks the real question.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import cannot reach a third-party network client
                continue
            module = node.module or ""
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def test_there_are_source_files_to_scan():
    """A scan over an empty file list passes vacuously, which would be the worst possible
    way for these tests to be green."""
    assert len(python_sources()) >= 20


@pytest.mark.parametrize("path", python_sources(), ids=lambda p: str(p.relative_to(SRC_DIR)))
def test_no_source_file_imports_a_network_client(path):
    """The mechanical proof of "no network client ships here".

    `LiveTransport` is where a real client would plug in, and it raises NotImplementedError
    rather than shipping a half-written one. This test is what keeps that true.
    """
    offenders = imported_modules(path) & NETWORK_MODULES
    assert offenders == set(), f"{path.relative_to(REPO_ROOT)} imports {sorted(offenders)}"


@pytest.mark.parametrize("path", python_sources(), ids=lambda p: str(p.relative_to(SRC_DIR)))
def test_no_source_file_reaches_the_network_through_a_dynamic_import(path):
    """`importlib.import_module("requests")` would slip past the AST import scan above."""
    source = path.read_text(encoding="utf-8")
    for module in NETWORK_MODULES:
        assert f'import_module("{module}"' not in source
        assert f"__import__('{module}'" not in source
        assert f'__import__("{module}"' not in source


# --------------------------------------------------------------------------------------
# no credentials anywhere in the repository
# --------------------------------------------------------------------------------------

SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}

# Patterns are assembled from fragments so that this file does not match itself.
CREDENTIAL_PATTERNS = {
    "private key header": re.compile("-----BEGIN" + r"[A-Z ]*PRIVATE" + " KEY-----"),
    "aws access key id": re.compile(r"\bAKIA" + r"[0-9A-Z]{16}\b"),
    "openai style key": re.compile(r"\bsk-" + r"[A-Za-z0-9]{20,}\b"),
    "github token": re.compile(r"\bgh[pousr]_" + r"[A-Za-z0-9]{36}\b"),
    "slack token": re.compile(r"\bxox[baprs]-" + r"[A-Za-z0-9-]{10,}\b"),
    "credential assignment": re.compile(
        r"(?i)\b(api[_-]?key|secret|password|passwd|access[_-]?token|auth[_-]?token"
        r"|client[_-]?secret)\s*[:=]\s*[\"'][^\"'\n]{8,}[\"']"
    ),
}


def repo_text_files() -> list:
    out = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or set(path.parts) & SKIP_DIRS:
            continue
        try:
            path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        out.append(path)
    return sorted(out)


def test_there_are_repository_files_to_scan():
    assert len(repo_text_files()) >= 20


def test_no_file_in_the_repository_contains_a_credential_shaped_string():
    """A key committed once is a key that has to be rotated, and the commit history keeps
    it forever. Config here may only reference `${VAR}`; a literal is refused at load time.
    """
    hits: list[str] = []
    for path in repo_text_files():
        text = path.read_text(encoding="utf-8")
        for label, pattern in CREDENTIAL_PATTERNS.items():
            for match in pattern.finditer(text):
                hits.append(f"{path.relative_to(REPO_ROOT)}: {label} near {match.start()}")
    assert hits == [], "\n".join(hits)


def test_the_config_loader_refuses_a_literal_credential():
    """The mechanism behind the scan above: refusing rather than warning is the control,
    because a warning gets scrolled past and a secret in a config file gets committed."""
    from ocm.config import ConfigError, resolve_env_ref

    with pytest.raises(ConfigError, match="a literal value is refused"):
        resolve_env_ref("hunter2-actual-secret", strict=True)
    assert resolve_env_ref("${SOME_VAR}", strict=False) == ""


def test_the_approval_key_is_never_read_from_config(monkeypatch):
    """Signing keys come from the environment or from an ephemeral in-process key. There
    is no config path that supplies one.
    """
    from ocm.approval.errors import ApprovalError
    from ocm.approval.tokens import ENV_KEY, require_env_key

    monkeypatch.delenv(ENV_KEY, raising=False)
    with pytest.raises(ApprovalError, match="refusing to proceed"):
        require_env_key()

    for path in CONFIG_DIR.glob("*.toml"):
        text = path.read_text(encoding="utf-8")
        assert ENV_KEY not in text
        assert "approval_key" not in text


# --------------------------------------------------------------------------------------
# a full organic round cannot publish for real
# --------------------------------------------------------------------------------------


def test_a_full_auto_round_publishes_only_dry_run_records():
    """Both graduation latches on, the loop closes end to end, and EVERY record it
    produced is marked dry_run. There is no `--live` flag: the dry run is not a mode that
    could be flipped, it is the only mode that exists.
    """
    loop, _, transport = _build_organic(
        CONFIG_DIR / "organic.toml", CONFIG_DIR / "rubric.toml"
    )
    loop.gate_mode = "auto"
    loop.auto_approval_enabled = True

    result = loop.run_round(0, now=FIXED_NOW)

    assert result.published, "the round published nothing, so the assertion below is vacuous"
    assert all(record.dry_run is True for record in result.published)
    assert transport.is_dry_run is True
    assert all(r.external_id.startswith("dryrun-") for r in result.published)


def test_every_transport_request_in_a_full_round_went_to_the_dry_run_stub():
    loop, _, transport = _build_organic(
        CONFIG_DIR / "organic.toml", CONFIG_DIR / "rubric.toml"
    )
    loop.gate_mode = "auto"
    loop.auto_approval_enabled = True
    loop.run_round(0, now=FIXED_NOW)

    assert transport.requests
    assert {r.operation for r in transport.requests} <= {"publish", "collect"}


def test_the_organic_loop_defaults_to_a_dry_run_transport():
    """No constructor argument, no environment variable, and no config key selects a live
    transport, so the default cannot be flipped by accident."""
    _, _, transport = _build_organic(CONFIG_DIR / "organic.toml", CONFIG_DIR / "rubric.toml")
    assert transport.is_dry_run is True


# --------------------------------------------------------------------------------------
# the paid platform cannot spend
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post", "put", "delete"])
def test_every_non_get_is_refused_without_a_recorded_human_confirmation(method):
    """Written as `method.upper() != "GET"` and not as `== "POST"`, so a later edit that
    narrows the check cannot accidentally reopen PUT, PATCH, or DELETE."""
    platform = DryRunPlatform(account_confirmed=False)
    with pytest.raises(PlatformRefused, match="no recorded human confirmation"):
        platform.request(method, "/campaigns")


def test_a_get_is_allowed_without_confirmation():
    """Reading is outside the money path, so it is not gated the same way."""
    assert DryRunPlatform(account_confirmed=False).request("GET", "/campaigns") == {"ok": True}


def test_a_refused_request_is_still_recorded_so_it_is_visible():
    platform = DryRunPlatform(account_confirmed=False)
    with pytest.raises(PlatformRefused):
        platform.request("POST", "/campaigns")
    assert platform.requests == [("POST", "/campaigns", {})]


def test_creating_a_campaign_is_refused_without_confirmation():
    platform = DryRunPlatform(account_confirmed=False)
    with pytest.raises(PlatformRefused):
        platform.create_paused(
            intent_digest="a" * 32,
            lifetime_budget_minor=25_000,
            currency="USD",
            starts_at="s",
            ends_at="e",
        )


def test_a_created_campaign_is_born_paused():
    """Written at the call site, never defaulted."""
    platform = DryRunPlatform(account_confirmed=True)
    state = platform.create_paused(
        intent_digest="a" * 32,
        lifetime_budget_minor=25_000,
        currency="USD",
        starts_at="s",
        ends_at="e",
    )
    assert state.status == "PAUSED"


@pytest.mark.parametrize("status", ["ACTIVE", "active", "ENABLED", "LIVE", "RUNNING", "DELIVERING"])
def test_set_status_refuses_every_live_status(status):
    """The strongest version of a spend gate is not a better check on the activation path;
    it is having NO activation path, so turning delivery on requires a human in the ad
    platform's own console.
    """
    platform = DryRunPlatform(account_confirmed=True)
    state = platform.create_paused(
        intent_digest="a" * 32, lifetime_budget_minor=25_000, currency="USD",
        starts_at="s", ends_at="e",
    )
    with pytest.raises(PlatformRefused, match="turned on by a human"):
        platform.set_status(state.platform_campaign_id, status)
    assert platform.get_state(state.platform_campaign_id).status == "PAUSED"


@pytest.mark.parametrize("status", ["PAUSED", "ARCHIVED"])
def test_set_status_accepts_only_the_two_non_delivering_states(status):
    platform = DryRunPlatform(account_confirmed=True)
    state = platform.create_paused(
        intent_digest="a" * 32, lifetime_budget_minor=25_000, currency="USD",
        starts_at="s", ends_at="e",
    )
    assert platform.set_status(state.platform_campaign_id, status).status == status


def test_the_platform_class_exposes_no_activation_method():
    """Stated structurally as well as behaviorally: there is no method to call, so there
    is nothing for a future caller to reach for."""
    forbidden = {"activate", "enable", "resume", "unpause", "start", "go_live", "launch", "spend"}
    assert forbidden & set(dir(DryRunPlatform)) == set()


def test_no_module_level_activation_function_exists():
    import ocm.paid.platform as platform_mod

    public = {name for name in dir(platform_mod) if not name.startswith("_")}
    assert {"activate", "activate_campaign", "go_live", "set_live"} & public == set()


def test_creating_twice_with_the_same_digest_adopts_rather_than_duplicates():
    """This is what the deterministic intent digest buys: a timed-out retry finds the
    campaign the first attempt may have created instead of creating a second real one."""
    platform = DryRunPlatform(account_confirmed=True)
    args = dict(
        intent_digest="a" * 32, lifetime_budget_minor=25_000, currency="USD",
        starts_at="s", ends_at="e",
    )
    first = platform.create_paused(**args)
    second = platform.create_paused(**args)

    assert first.platform_campaign_id == second.platform_campaign_id
    assert len(platform.find_by_digest("a" * 32)) == 1
    assert len([r for r in platform.requests if r[0] == "POST"]) == 1


def test_an_ambiguous_duplicate_state_is_escalated_not_resolved_by_another_create():
    """Two campaigns carrying one digest is a state a human has to untangle. Creating a
    third would make it worse while looking like progress."""
    from ocm.paid.platform import OutcomeUnknown

    platform = DryRunPlatform(account_confirmed=True)
    for suffix in ("one", "two"):
        platform._campaigns[f"camp-{suffix}"] = CampaignState(
            platform_campaign_id=f"camp-{suffix}",
            status="PAUSED",
            intent_digest="a" * 32,
            lifetime_budget_minor=25_000,
            currency="USD",
            starts_at="s",
            ends_at="e",
        )

    with pytest.raises(OutcomeUnknown, match="must be resolved by a human"):
        platform.create_paused(
            intent_digest="a" * 32, lifetime_budget_minor=25_000, currency="USD",
            starts_at="s", ends_at="e",
        )


def test_outcome_unknown_is_not_a_refusal_and_a_refusal_is_not_retryable():
    """They mean opposite things to a caller: one says do not retry because it may have
    landed, the other says this was refused outright."""
    from ocm.paid.platform import OutcomeUnknown

    assert not issubclass(OutcomeUnknown, PlatformRefused)
    assert not issubclass(PlatformRefused, OutcomeUnknown)


def test_reading_insights_is_a_get_and_needs_no_confirmation():
    platform = DryRunPlatform(account_confirmed=True)
    state = platform.create_paused(
        intent_digest="a" * 32, lifetime_budget_minor=25_000, currency="USD",
        starts_at="s", ends_at="e",
    )
    platform.account_confirmed = False
    platform.requests.clear()

    rows = platform.insights(state.platform_campaign_id, days=3)
    assert len(rows) == 3
    assert {method for method, _, _ in platform.requests} == {"GET"}


def test_there_is_no_live_flag_anywhere_in_the_cli():
    """A flag that flips the mode is a flag someone eventually flips."""
    cli_source = (SRC_DIR / "ocm" / "cli.py").read_text(encoding="utf-8")
    for flag in ('"--live"', "'--live'", '"--production"', '"--real"'):
        assert flag not in cli_source
