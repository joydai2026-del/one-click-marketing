"""Command line entry point. Everything it does is a dry run.

    ocm demo            run the full loop end to end, organic then paid
    ocm organic         organic rounds only
    ocm approve         the human-approval path: park a draft, approve it, publish it
    ocm paid            paid loop only: intent, review card, spend gate, collection
    ocm rubric          print the loaded rubric

There is no `--live` flag and no credential path, in either loop. This repository ships no
network client, so the dry run is not a mode that could be flipped: it is the only mode
that exists.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as cfgmod
from .approval.ledger import InMemoryLedger
from .approval.tokens import ephemeral_key, issue
from .channels.registry import build_channels
from .channels.transport import DryRunTransport
from .evaluation.compliance import ComplianceFloor
from .evaluation.dedup import DedupIndex
from .evaluation.gate import QualityGate
from .evaluation.rubric import Rubric, stub_scorer
from .generation.generator import FactBank, Generator, TemplateGenerator
from .generation.style import StyleSpace
from .learning.ranker import Ranker, Tilt
from .loop import OrganicLoop
from .paid import (
    Campaign,
    Collector,
    DryRunPlatform,
    SnapshotStore,
    SpendRefused,
    authorize,
    intent_digest,
    load_creatives,
    render_review_card,
)
from .publishing import PostLog, SchedulePolicy

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ORGANIC = _ROOT / "config" / "example" / "organic.toml"
_DEFAULT_RUBRIC = _ROOT / "config" / "example" / "rubric.toml"
_DEFAULT_CAMPAIGN = _ROOT / "config" / "example" / "campaign.toml"

BANNER = """\
================================================================================
 ONE-CLICK MARKETING LOOP  ::  DRY RUN
 Every transport is a stub. No network call is made, no credential is read, no
 content is published, and no money can be spent. All content below is
 fabricated placeholder text generated offline.
================================================================================"""


def _build_organic(organic_path: Path, rubric_path: Path):
    conf = cfgmod.load(organic_path)
    rubric = Rubric.from_config(cfgmod.load_raw(rubric_path))
    compliance = ComplianceFloor.from_config(conf.optional("compliance"))
    dedup = DedupIndex.from_config(conf.optional("dedup"))
    gate = QualityGate(
        rubric=rubric, compliance=compliance, dedup=dedup, scorer=stub_scorer()
    )
    transport = DryRunTransport()
    channels = build_channels(conf.data, transport)
    space = StyleSpace.from_config(conf.section("style"))
    bank = FactBank.from_config(conf.section("bank"))
    loop_cfg = conf.optional("loop")
    return (
        OrganicLoop(
            space=space,
            bank=bank,
            generator=Generator(model=TemplateGenerator(), compliance=compliance),
            gate=gate,
            channels=channels,
            ranker=Ranker.from_config(conf.optional("learning")),
            tilt=Tilt(),
            schedule=SchedulePolicy.from_config(conf.optional("schedule")),
            log=PostLog(),
            gate_mode=str(loop_cfg.get("gate_mode", "human")),
            auto_approval_enabled=bool(loop_cfg.get("auto_approval_enabled", False)),
        ),
        rubric,
        transport,
    )


def cmd_organic(args) -> int:
    print(BANNER)
    loop, rubric, transport = _build_organic(Path(args.organic), Path(args.rubric))

    print(f"\nrubric          {rubric.version}, {len(rubric.dimensions)} dimensions, "
          f"threshold {rubric.threshold}")
    print(f"style space     {loop.space.size} points across {loop.space.dimensions}")
    cycle, positions = loop.space.exploration_positions()
    print(f"exploration     {len(positions)} of every {cycle} positions reserved")
    print(f"gate mode       {loop.gate_mode} "
          f"(auto_approval_enabled={loop.auto_approval_enabled})")

    if loop.gate_mode == "human" and not args.auto:
        print(
            "\nNOTE: gate_mode is 'human', so drafts are generated, evaluated, and then\n"
            "      PARKED at the approval boundary. Nothing publishes without a person.\n"
            "      Parked drafts are held by content hash, so an approval supplied later\n"
            "      still applies to the exact bytes that were reviewed.\n"
            "      Run `ocm approve` to see a parked draft approved and published.\n"
            "      Run with --auto to see the unattended loop close instead."
        )

    if args.auto:
        loop.gate_mode = "auto"
        loop.auto_approval_enabled = True
        print("\n--auto: both graduation latches set for this process only.")

    # The schedule cap is one confirmed post per channel per 24h, so each round is walked
    # forward a day. A fixed base timestamp keeps the whole run reproducible.
    base_now = 1_800_000_000.0
    learnings: dict = {}
    for r in range(args.rounds):
        now = base_now + r * 86_400
        print(f"\n{'-' * 78}\nROUND {r}  (simulated day {r})")
        result = loop.run_round(r, learnings=learnings, now=now)
        learnings = result.learnings

        for d in result.drafted:
            print(f"  drafted   {d.draft_id}  [{', '.join(d.derived_from)}]")
        for ev in result.evaluations:
            mark = "PASS" if ev.passed else "FAIL"
            print(f"  gate      {ev.draft_id}  {mark}  score {ev.weighted_score:.2f}/"
                  f"{ev.threshold:.2f}")
            for hf in ev.hard_failures:
                print(f"              hard floor: {hf}")
        for note in result.notes:
            print(f"  note      {note}")
        for h in result.held:
            print(f"  held      {h}")
        for s in result.skipped:
            print(f"  skipped   {s}")
        for p in result.published:
            print(f"  published {p.channel}  {p.external_id}  (dry_run={p.dry_run})")
        for pid, e in result.engagement.items():
            print(f"  collected {pid}  {e.metrics}")
        for channel, dims in result.learnings.items():
            for dim, learn in sorted(dims.items()):
                print(f"  learned   {channel}/{dim}: {learn.status}"
                      + (f" -> {learn.winner!r}" if learn.winner else "")
                      + f"  (n={learn.sample_count})")

    print(f"\n{'-' * 78}")
    print(f"transport requests recorded: {len(transport.requests)} "
          f"(publish={len(transport.requests_for('publish'))}, "
          f"collect={len(transport.requests_for('collect'))})")
    print("every one of them was a dry-run stub call; nothing left this process.")
    return 0


def cmd_paid(args) -> int:
    print(BANNER)
    conf = cfgmod.load(Path(args.campaign))
    campaign = Campaign.from_config(conf.data, source_dir=conf.source_dir)

    starts = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    ends = starts + timedelta(days=campaign.run_days)
    digest = intent_digest(campaign, starts_at=starts, ends_at=ends)

    print("\n1. INTENT DIGEST")
    print("   Deterministic over every field that changes what is bought. Excludes the")
    print("   config's directory, so the same campaign digests identically from any")
    print("   checkout: that is what lets a timed-out retry ADOPT the first attempt's")
    print("   campaign instead of creating a second real one.")
    print(f"   digest: {digest}")
    again = intent_digest(campaign, starts_at=starts, ends_at=ends)
    print(f"   recomputed: {again}   identical: {digest == again}")

    print("\n2. CREATIVES")
    reads, errors = load_creatives(campaign.creatives, campaign.source_dir)
    for r in reads:
        status = "ok" if r.ok else f"REFUSED ({r.error})"
        print(f"   {r.ref}\n     -> {status}")
    if errors:
        print("   refusing to create anything: unmet creatives above")
        return 1

    print("\n3. REVIEW CARD (what a human reads)")
    card = render_review_card(campaign, starts_at=starts, ends_at=ends, digest=digest)
    print("\n".join("   " + line for line in card.splitlines()))

    platform = DryRunPlatform()
    platform.account_confirmed = True  # dry-run only; a real account needs a human record
    state = platform.create_paused(
        intent_digest=digest,
        lifetime_budget_minor=campaign.guardrails.lifetime_budget_minor,
        currency=campaign.guardrails.currency,
        starts_at=starts.isoformat(),
        ends_at=ends.isoformat(),
    )
    print(f"\n4. CREATED  {state.platform_campaign_id}  status={state.status}")
    print("   Born PAUSED, written at the call site. There is no activate() in this")
    print("   codebase and no path that sets a live status. Turning delivery on is a")
    print("   human action in the ad platform's own console.")

    adopted = platform.create_paused(
        intent_digest=digest,
        lifetime_budget_minor=campaign.guardrails.lifetime_budget_minor,
        currency=campaign.guardrails.currency,
        starts_at=starts.isoformat(),
        ends_at=ends.isoformat(),
    )
    print(f"   retry of the same create -> adopted {adopted.platform_campaign_id} "
          f"(duplicate campaigns: {len(platform.find_by_digest(digest)) - 1})")

    print("\n5. SPEND GATE")
    key = ephemeral_key()
    ledger = InMemoryLedger()
    token, sig = issue(
        scope=f"spend:{campaign.campaign_id}",
        content_hash=digest,
        subject=state.platform_campaign_id,
        key=key,
        max_spend_minor=campaign.guardrails.lifetime_budget_minor,
        approver="demo-operator",
    )
    grant = authorize(
        campaign=campaign,
        token=token,
        signature=sig,
        key=key,
        ledger=ledger,
        live_state=state,
        creative_reads=reads,
        expected_intent_digest=digest,
        intended_spend_minor=campaign.guardrails.lifetime_budget_minor,
        expected_starts_at=state.starts_at,
        expected_ends_at=state.ends_at,
    )
    print(f"   authorized: ceiling {grant.approved_ceiling_minor} minor units, "
          f"approver {grant.approver}")

    print("   replaying the same approval a second time:")
    try:
        authorize(
            campaign=campaign,
            token=token,
            signature=sig,
            key=key,
            ledger=ledger,
            live_state=state,
            creative_reads=reads,
            expected_intent_digest=digest,
            intended_spend_minor=campaign.guardrails.lifetime_budget_minor,
        )
        print("   ERROR: replay was accepted")  # pragma: no cover
        return 1
    except SpendRefused as exc:
        print(f"     refused: {exc.reasons[0]}")

    print("   presenting the same approval against a CHANGED intent:")
    try:
        authorize(
            campaign=campaign,
            token=token,
            signature=sig,
            key=key,
            ledger=InMemoryLedger(),
            live_state=state,
            creative_reads=reads,
            expected_intent_digest="0" * 32,
            intended_spend_minor=campaign.guardrails.lifetime_budget_minor,
        )
        print("   ERROR: changed intent was accepted")  # pragma: no cover
        return 1
    except SpendRefused as exc:
        print(f"     refused: {exc.reasons[0]}")

    print("\n6. RESULTS COLLECTION")
    store = SnapshotStore()
    collector = Collector(platform=platform, store=store)
    written, errs = collector.collect(
        campaign_id=campaign.campaign_id,
        platform_campaign_id=state.platform_campaign_id,
        currency=campaign.guardrails.currency,
        now=1_800_000_000.0,
    )
    print(f"   appended {written} snapshot(s), {len(errs)} row error(s)")
    written2, _ = collector.collect(
        campaign_id=campaign.campaign_id,
        platform_campaign_id=state.platform_campaign_id,
        currency=campaign.guardrails.currency,
        now=1_800_003_600.0,
    )
    print(f"   re-collected the trailing window: {written2} more row(s) appended, "
          f"total stored {len(store.all_rows())}")
    print(f"   latest-effective view resolves to {len(store.latest_effective())} current "
          f"reading(s): append-only storage plus read-time resolution, so a restatement")
    print("   never destroys the evidence of what was known at decision time.")
    unknown = sum(1 for r in store.latest_effective() if r.purchases is None)
    print(f"   readings with purchases UNKNOWN (None, not 0): {unknown}")
    print(f"   total spend across current readings: {store.total_spend_minor()} minor units")

    non_get = [r for r in platform.requests if r[0] != "GET"]
    print(f"\n   platform requests: {len(platform.requests)} total, "
          f"{len(non_get)} non-GET (all creates, all producing PAUSED objects)")
    return 0


def cmd_approve(args) -> int:
    """Walk the human-approval path: park a draft, approve it, watch it publish.

    This is the shipped default's happy path, and the thing the repository actually claims.
    "The machine stops" is only half of it; the other half is that a person can say yes and
    have that yes be bound to the exact bytes they looked at.
    """
    print(BANNER)
    loop, _, transport = _build_organic(Path(args.organic), Path(args.rubric))
    now = 1_800_000_000.0

    print("\n1. ROUND ONE, gate_mode=human")
    first = loop.run_round(0, now=now)
    print(f"   drafted   {len(first.drafted)}")
    print(f"   published {len(first.published)}   <- nothing, by design")
    print(f"   parked    {len(loop.pending)} draft(s) awaiting a human")
    if not loop.pending:
        print("   nothing was parked, so there is nothing to approve")
        return 1

    chash, draft = next(iter(loop.pending.items()))
    print("\n2. WHAT THE HUMAN REVIEWS")
    print(f"   draft        {draft.draft_id}")
    print(f"   channel      {draft.channel}")
    print(f"   content hash {chash}")
    body = " ".join(draft.body.split())
    print(f"   body         {body[:150]}{'...' if len(body) > 150 else ''}")

    print("\n3. THE HUMAN APPROVES")
    token, sig = issue(
        scope=f"publish:{draft.channel}",
        content_hash=chash,
        subject=draft.draft_id,
        key=loop.approval_key,
        approver="a-human-at-a-console",
    )
    loop.approvals[chash] = (token, sig)
    print(f"   approval bound to {token.content_hash}")
    print(f"   scope             {token.scope}")
    print(f"   expires           {token.expires_at - token.issued_at:.0f}s after issue")

    print("\n4. ROUND TWO: the parked draft publishes")
    second = loop.run_round(1, now=now)
    for r in second.published:
        print(f"   published {r.channel}  {r.external_id}  (dry_run={r.dry_run})")
    if not second.published:
        print("   ERROR: the approved draft did not publish")
        return 1

    print("\n5. THE SAME APPROVAL, PRESENTED AGAIN")
    loop.approvals[chash] = (token, sig)
    loop.pending[chash] = draft
    third = loop.run_round(2, now=now + 25 * 3600)
    replayed = [r for r in third.published if r.draft_id == draft.draft_id]
    print(f"   republished the approved draft: {len(replayed)}  <- single use, so zero")

    print(f"\n   transport publish calls: {len(transport.requests_for('publish'))}")
    return 0 if not replayed else 1


def cmd_rubric(args) -> int:
    rubric = Rubric.from_config(cfgmod.load_raw(Path(args.rubric)))
    print(f"rubric {rubric.version}   pass threshold {rubric.threshold} / 5.0\n")
    total = sum(d.weight for d in rubric.dimensions)
    for d in rubric.dimensions:
        floor = f"floor {d.floor}" if d.floor is not None else "compensable"
        print(f"  {d.name:26s} weight {d.weight:>4} ({d.weight / total:5.1%})  {floor}")
        print(f"    {d.description}")
    return 0


def cmd_demo(args) -> int:
    rc = cmd_organic(args)
    if rc:
        return rc
    print("\n\n")
    return cmd_paid(args)


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--organic", default=str(_DEFAULT_ORGANIC))
    common.add_argument("--rubric", default=str(_DEFAULT_RUBRIC))
    common.add_argument("--campaign", default=str(_DEFAULT_CAMPAIGN))
    common.add_argument("--rounds", type=int, default=4)
    common.add_argument(
        "--auto",
        action="store_true",
        help="set both auto-approval latches for this process so the organic loop closes",
    )

    parser = argparse.ArgumentParser(
        prog="ocm",
        description="One-click marketing loop, dry-run reference implementation.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("demo", parents=[common]).set_defaults(func=cmd_demo)
    sub.add_parser("organic", parents=[common]).set_defaults(func=cmd_organic)
    sub.add_parser("paid", parents=[common]).set_defaults(func=cmd_paid)
    sub.add_parser("approve", parents=[common]).set_defaults(func=cmd_approve)
    sub.add_parser("rubric", parents=[common]).set_defaults(func=cmd_rubric)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func = cmd_demo
        args.auto = True
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
