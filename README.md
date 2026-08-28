# One-Click Marketing Loop

A distilled, dependency-free reference implementation of a closed marketing loop: **content
generation, quality gating, human approval, distribution, results collection, and learning
that feeds the next round.** Organic accounts and paid campaigns share one spine.

**A human approves before anything publishes or spends, and the approval is
cryptographically bound to the exact content that was reviewed.** The approval mechanism is
implemented and tested end to end. The review *surface* a person would use (a console, a
chat, a web form) is out of scope here: this repository is the engine behind it, and a
supplied signed token stands in for the click.

Everything here runs offline. **This repository ships no network client**, every transport
is a stub, no command reads a platform credential, and nothing it can do publishes content or
moves money.

```bash
git clone https://github.com/joydai2026-del/one-click-marketing
cd one-click-marketing
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/python -m ocm.cli demo      # the whole loop, organic then paid
.venv/bin/python -m ocm.cli organic   # organic rounds only
.venv/bin/python -m ocm.cli approve   # park a draft, approve it, watch it publish
.venv/bin/python -m ocm.cli paid      # intent, review card, spend gate, collection
.venv/bin/python -m ocm.cli rubric    # print the loaded quality rubric
.venv/bin/python -m pytest            # the test suite
```

Requires Python 3.11 or newer. **Zero runtime dependencies.**

---

## The loop

```mermaid
flowchart LR
    subgraph ORGANIC["Organic loop"]
        direction LR
        SCHED[Schedule check<br/>one post per channel<br/>per window] --> GEN[Generate<br/>grounded in a fact bank<br/>inside a typed style space]
        GEN --> HOLD{Identical across<br/>channels?}
        HOLD -->|yes| HELD[Held for a human]
        HOLD -->|no| GATE{Quality gate}
        GATE -->|hard floor tripped<br/>or below threshold| BLOCK[Blocked]
        GATE -->|passes| APPR{{Human approval<br/>bound to content hash}}
        APPR -->|signed approval supplied| PUB[Publish<br/>via channel adapter]
        APPR -->|none yet| WAIT[Parked by content hash.<br/>Nothing publishes.]
        WAIT -.->|approval arrives later| APPR
        PUB --> COLL[Collect engagement]
        COLL --> LEARN[Rank style dimensions<br/>per channel]
    end
    LEARN -.->|winners, or an honest<br/>'no winner yet'| GEN

    subgraph PAID["Paid loop"]
        direction LR
        CAMP[Campaign config] --> DIG[Intent digest<br/>deterministic over<br/>everything that is bought]
        DIG --> CARD[Review card<br/>what the human reads]
        CARD --> SIGN{{Signed approval<br/>bound to the digest}}
        SIGN --> CREATE[Create campaign<br/>born PAUSED]
        CREATE --> SGATE{Spend gate<br/>re-check every bound field<br/>against LIVE state}
        SGATE -->|any mismatch| REFUSE[Refused<br/>all reasons at once]
        SGATE -->|all clear| GRANT[SpendGrant minted]
        GRANT --> RES[Collect results<br/>append-only snapshots]
    end
    RES -.->|informs the next round<br/>(operator-driven, not automated here)| CAMP

    style APPR fill:#2d5016,color:#fff
    style SIGN fill:#2d5016,color:#fff
    style GATE fill:#4a3800,color:#fff
    style SGATE fill:#4a3800,color:#fff
    style BLOCK fill:#5a1a1a,color:#fff
    style REFUSE fill:#5a1a1a,color:#fff
    style HELD fill:#5a1a1a,color:#fff
    style WAIT fill:#4a3800,color:#fff
```

---

## What is actually interesting here

Most of this is ordinary plumbing. These are the parts that took real thought, each of them
a defense against a specific way an always-on marketing loop goes wrong.

### 1. An approval is bound to content, not to intent

`src/ocm/approval/tokens.py`

"A human approved it" is worth very little if the thing approved can change afterwards. An
operator reviews draft A, clicks approve, the pipeline regenerates into draft B, and the
approval now authorizes something no human ever saw.

So an approval token is an HMAC over a canonical payload that includes the **hash of the
exact bytes shown to the human**. At publish or spend time the caller recomputes the hash of
what it is *about to send*. One changed character and the token is refused.

Six independent checks, each catching a different failure:

| Check | Catches | Raises |
|---|---|---|
| signature | forged or tampered token | `SignatureError` |
| expiry | a stale approval used months later | `ExpiredError` |
| scope | a publish approval reused to authorize spend | `ScopeMismatchError` |
| content | approved A, about to send B | `ContentMismatchError` |
| ceiling | spending more than was approved | `SpendLimitError` |
| nonce | the same approval used twice | `ReplayError` |

Three ordering decisions carry the weight:

- **The signature is checked first**, with `hmac.compare_digest`. Every other field is
  attacker-controlled until the signature says otherwise.
- **The nonce is consumed last**, so a token rejected for being expired or misscoped is not
  silently burned.
- **Domains are separated** with a NUL-terminated prefix, so bytes signed as a publish
  approval cannot be re-presented as a spend approval.

### 2. The spend gate does not stop at a valid signature

`src/ocm/paid/spend_gate.py`

A valid token proves a human approved an *intent*. It does not prove the campaign sitting on
the ad platform right now is still that intent. Between approval and spend, a campaign can be
deleted and recreated under the same id with a different budget.

So every bound field is re-compared against **live platform state** at spend time: subject,
intent digest, budget, currency, flight start and end, and the creatives re-hashed off disk.
Flight dates sit inside the digest already, so they are checked twice on purpose: the digest
is a derived value, and the direct field comparison is the observation to trust if the two
ever disagree.

- **An empty live digest refuses.** It does not pass. A campaign this system cannot prove
  anything about resolves to no.
- **A live status that is not `PAUSED` refuses.** A campaign already delivering is not one
  awaiting authorization; approving it rubber-stamps money that is already moving.
- **The creatives are re-hashed here, from their bytes.** A `CreativeRead` is an ordinary
  object a caller constructs, so trusting its own "I'm fine" flag would make the last line
  of defense depend on the honesty of what it defends against. They are also compared ref by
  ref rather than as a set of hashes, so one asset presented twice cannot satisfy a
  two-asset approval.
- **The nonce is burned last, after the live checks too.** Verification stops short of
  consuming it, so a transient platform mismatch refuses without destroying a valid human
  approval and forcing a person to re-issue it.
- **All mismatches are reported together.** Raising on the first one turns a five-field
  problem into five review cycles.
- **The result is a capability, not a boolean.** `authorize` returns a frozen `SpendGrant`
  whose constructor refuses to build one without a module-private sentinel. A boolean can be
  shadowed by a later `= True`; this cannot be produced by accident. It is a guard against
  mistakes, not a security boundary: Python has no private state, so a determined caller can
  import the sentinel. The value is that every *accidental* path to a grant fails loudly.

### 3. The intent digest is deterministic, and excludes anything machine-specific

`src/ocm/paid/intent.py`

A random approval nonce means a retry after a timeout computes a different value, the first
attempt's campaign becomes unfindable, and the retry creates a **second real campaign
spending real money**. A deterministic digest lets the retry *adopt* what the first attempt
may have created. Determinism here is a double-spend control, not a convenience.

Fields are **length-prefixed** before hashing rather than joined with a separator. A
separator only works if no field can contain it, and nothing enforced that: `("a\x1fb", "c")`
and `("a", "b\x1fc")` join to identical bytes, and on a spend gate a digest collision means
an approval for one campaign authorizing another. A length prefix removes the assumption
instead of restating it.

The digest covers everything that changes what is bought (budget, flight, geo, sorted
creative hashes) and deliberately **excludes the config file's directory path**.
Including it would make the same campaign digest differently from two checkouts, recreating
the exact duplicate-campaign bug the determinism exists to prevent. Timestamps are
canonicalized to UTC and truncated to the minute for the same reason.

### 4. Campaigns are born paused, and there is no way to unpause them

`src/ocm/paid/platform.py`

Every created campaign is `PAUSED`, written explicitly at the call site and never defaulted.
There is no `activate()` method and no module-level activation function anywhere in this
repository.

The strongest version of a spend gate is not a better check on the activation path. It is
having no activation path at all, so turning delivery on requires a human in the ad
platform's own console. That is why anyone can clone and run this with no possibility of it
spending money.

The transport refuses non-GET requests written as `method.upper() != "GET"`, not
`== "POST"`, so a later narrowing edit cannot accidentally reopen PUT, PATCH, or DELETE. A
gate should fail toward refusing.

### 5. The learning loop refuses to invent a winner

`src/ocm/learning/ranker.py`

The characteristic failure of an unsupervised optimization loop is that it finds a random
early winner, plays it forever, and its operator reads the resulting flat line as a plateau
rather than as a bug.

So content is generated inside an explicit **style space** (hook x angle x format), every
draft carries its coordinates, and the ranker reports per dimension whether one value
actually beat the others. It declines in three distinct ways, because they call for different
responses:

| Status | Meaning | What to do |
|---|---|---|
| `insufficient_data` | fewer than `min_samples` measurements | keep gathering |
| `low_confidence` | only one distinct value was ever tried, so nothing was compared against anything | the rotation is not spreading across this axis |
| `no_separation` | two or more values were compared and came out level | these options are equivalent so far |

All three yield `winner=None`, and every consumer refuses to tilt on a `None` winner. A
test asserts all three are actually reachable, because a status the code can never produce
is documentation of a decision it does not make.

**Absent is not zero.** A published item with no measurement yet is *dropped* from the
sample, never scored `0.0`. Treating "not measured" as "measured badly" teaches the loop to
avoid whatever the collector is currently failing to read.

### 6. Exploration positions are derived, never hand-picked

`src/ocm/generation/style.py`

A loop that always plays the current winner stops learning immediately. So some positions
are reserved for pure exploration, where winners are ignored.

The trap is picking those positions by a round number like "every third tick". If an axis
happens to have 3 values, that resonates with it and explores the **same value every single
time**, so two of its three values are never explored at all. Nobody notices, because the
loop still looks busy.

So the schedule is derived from the axis lengths: the cycle is their lowest common multiple,
and the stride is the smallest step **above 1** that is **coprime** with that cycle. Two
properties come out of that, and they are different:

- **Coverage.** Each axis length divides the cycle, so a stride coprime with the cycle is
  coprime with each axis length, which means it generates that axis's whole value set.
  Reserving at least `max(axis_length)` positions therefore reaches every value of every
  axis.
- **Interleaving.** The search starts at 2, not 1. A stride of 1 is trivially coprime with
  everything, so starting at 1 would return 1 for every cycle, the coprimality test would
  never constrain anything, and the reserved positions would collapse to the contiguous
  prefix `[0, want)`. Coverage would still hold, so nothing would look broken, but the loop
  would explore in one burn-in block and then exploit for the rest of the cycle.

Add a value to any axis and the schedule recomputes itself. A property test walks nine axis
combinations, including the resonance trap, and asserts every value of every axis is covered.

### 7. Per-channel normalization, because raw engagement is not comparable

`src/ocm/channels/adapters.py`

A long-form newsletter reaches a roughly fixed subscriber list, so **absolute engagement** is
the signal. A public short-form feed shows a post to a wildly variable audience, so absolute
engagement mostly measures how the ranking system felt that hour and the signal is the
**rate**.

Ranking those on one leaderboard by raw counts lets a single viral short post drown out every
newsletter forever, and the loop learns the wrong lesson. So `normalize` lives on the
*adapter*, each channel converts its own counters to a 0-1 score, and the ranker never sees a
raw counter.

The short-form adapter also returns `0.0` below an impression floor. Three engagements on
eleven impressions is a 27 percent rate and means nothing.

### 8. The quality gate short-circuits, in a deliberate order

`src/ocm/evaluation/gate.py`

1. **compliance floors** (cheap, deterministic, non-compensable)
2. **dedup** (cheap, deterministic, non-compensable)
3. **rubric scoring** (an LLM call in production, compensable)

Steps 1 and 2 short-circuit. There is no point paying a judge to grade a draft that is
already disqualified, and no threshold high enough to make a compliance failure acceptable.
A test proves the scorer is never called when a floor trips.

The rubric is ten weighted dimensions loaded from `config/example/rubric.toml`. Two of them
carry a **floor**: a weighted average correctly lets good writing offset a dull opener, but
it must not let good writing offset an invented statistic or a missing disclosure.

The compliance floor includes a **configurable forbidden-substring check**, matched after
NFKC folding, invisible-character stripping, and whitespace collapse, so cosmetic evasion
does not work. The stripping is *categorical*: everything Unicode itself classifies as a
format or control character, not a hand-written list. That list started with six well-known
offenders and was evaded by U+200E LEFT-TO-RIGHT MARK, which was simply not on it. Any
enumeration is a list of the characters someone happened to think of, and the attacker only
has to find the one that was forgotten. Violations are reported **by index rather than by value**, because the term
list is the sensitive artifact and an audit log is a wider surface than a config file. The
term list is necessarily stated in the generation instruction (you cannot ask a model to avoid
something without naming it), but it is **masked out of the "avoid repeating these" block**
before recent history is shown back. Otherwise a term that once slipped into a published post
would be handed straight back to the model as an example of its own established voice.

### 9. Near-duplicate rejection, which a hash cannot do

`src/ocm/evaluation/dedup.py`

An always-on generator drifts toward its own greatest hits and re-posts a paraphrase of last
week's best performer. Exact-hash dedup does not catch this: one changed word is a new hash.
So there are two guards, because they fail differently: exact hash (catches a retry or a
double dispatch) and shingle-based Jaccard similarity against the published corpus (catches
the generator paraphrasing itself).

### 10. Identity comes from the slot, never from the generated text

`src/ocm/generation/identity.py`

Generation is non-deterministic. If a work item's identity is derived from its output, a
crash-and-retry produces a new id, the store does not recognize it, and the pipeline
publishes a second copy of something a human already saw.

So `slot_id = sha256(style_id | topic)` and identity is derived from the **slot inputs**. Re-run
the same tick and every id is identical, which is the property a durable store needs in order
to deduplicate a retry with a uniqueness constraint. Fields are NUL-separated so `("a.b", "c")`
and `("a", "b.c")` cannot collide, and a slot collision within one run raises loudly rather
than letting planned content silently vanish.

This repository provides the stable identity, not the durable store: the in-memory loop has no
insert-or-ignore step, so idempotency is *available* here rather than *enforced* here.

### 11. `posting` exists so a crash is not ambiguous

`src/ocm/publishing.py`

The publish state machine is an explicit transition table, and `evaluated -> published` is
not in it: nothing reaches a channel without passing through an approval.

`posting` is an intent row appended and **fsynced before** the network call, and matched by a
confirmation row after. Without it, a crash mid-publish is indistinguishable from a crash
before publish, and recovery has to guess. Guessing wrong loses a post in one direction and
double-posts in the other. `dangling_intents()` returns the intent rows with no matching
confirmation: exactly the possibly-published set a human must reconcile.

Only confirmed rows count toward the cap, so a failed publish never burns the day's slot.

An **indeterminate outcome is not a failure**. A timeout leaves the intent row standing and
is never auto-retried, because retrying an unknown outcome is how one approved post becomes
two. Deleting the row on the way out would destroy the only evidence that something may have
gone out. And a channel reporting success with no external id raises: that is a post the
system can never find, measure, or delete, which is worse than a failure.

### 12. Results storage is append-only, resolved at read time

`src/ocm/paid/collector.py`

Ad platforms **restate history**: a number for last Tuesday can change on Thursday. Upserting
destroys the record of what you knew when you made the decision. So every collection appends
a snapshot, and the current value is resolved by a latest-effective query keyed on
(ad, day, attribution window), newest source timestamp winning. A late-arriving *older*
restatement does not win.

**A partially known total is reported as unknown.** If any reading is missing,
`total_spend_minor()` returns `None` rather than the sum of the readable rows. Summing what
you can see and calling it a total is missing-is-not-zero one level up: it *understates*
spend, to the one caller whose job is to stop at a ceiling. `known_spend_minor()` returns the
partial figure plus a count of unreadable rows, for reporting where "incomplete" can be said
out loud.

Four more rules, each from a real failure mode:

- **Missing is `None`, never `0.0`.** A metric the platform did not report is not a
  measurement of zero. A genuine reported zero is kept as zero: those must stay
  distinguishable. `total_spend_minor()` returns `None` when nothing is readable, because a
  budget guard reading "0 spent" from "cannot see the spend" lets a campaign run forever.
- **Actions are found by name, never by position.** Indexing into the action list works right
  up until the order changes, at which point purchases are read from the wrong bucket with no
  error.
- **Spend is converted from major to minor units, never copied.** A copied figure is a
  hundredfold understatement that the budget guard would wave through.
- **The attribution window is pinned and stored on every row.** Inheriting the account default
  means a setting changed in a web UI silently redefines what every stored row *means*.

### 13. Money is integers, and the decision metric is pre-registered

`src/ocm/paid/campaign.py`

Budgets are integers in minor units. Float arithmetic on money produces a ceiling that is
occasionally a hundredth over, and "occasionally over the approved amount" is the one thing a
spend guard may not be.

Budgets are **lifetime, not daily**, because platforms treat a daily budget as a target they
may overshoot. A daily figure is not a cap and must not be presented as one.

`decision_metric` is required and non-empty: the number the round will be judged on is named
*before* any money moves. Choosing it afterwards from the results is how every campaign
becomes a success, because there is always some metric that went up.

Config keys are a **closed allowlist** and an unknown key raises. A typo'd key that is
silently ignored gives you a campaign running with a default nobody chose.

---

## Human in the loop

Two gate modes, both config-driven. The difference is precise:

| Mode | Behavior |
|---|---|
| `human` (shipped default) | drafts are **parked**, keyed by content hash. A signed approval supplied out of band publishes them on the next tick. |
| `auto` | may proceed without the human tap, but **only** for a draft that passed every check with zero flags. |

Parking is the part that makes the gate usable rather than merely obstructive. If each round
discarded its unapproved drafts and generated fresh ones, a reviewer's approval could never
land: by the time they said yes, the thing they reviewed would no longer exist anywhere in
the system. `ocm approve` walks the whole path, including the replay refusal at the end.

An out-of-band approval wins in **either** mode, and a supplied approval that fails to verify
is a stop, not a fallthrough to auto. If a person tried to approve something and the approval
did not check out, that is the one moment the machine must not decide for itself.

**Auto skips the tap, never the check.** A draft that fails in auto mode is left *pending* for
a human and is never auto-rejected: the machine is trusted to say "this is clean", not to say
"this is bad".

Both graduation latches ship **off**. `auto` requires `gate_mode = "auto"` *and* a separate
`auto_approval_enabled` flag, so a single edit cannot silently make the loop unattended. A
test asserts the shipped default publishes nothing.

---

## Module layout

```
src/ocm/
├── models.py            core records; everything crossing a stage carries a content hash
├── config.py            TOML/JSON loading; credentials may only be ${ENV_REF}, never literals
├── loop.py              the orchestrator: one full round, and the ordering rationale
├── publishing.py        publish state machine, at-most-once publishing, schedule policy
├── generation/
│   ├── style.py         the style space, deterministic rotation, coprime exploration schedule
│   ├── identity.py      slot-derived ids so a retry is idempotent
│   └── generator.py     grounded drafting, retry budget, forbidden-term masking
├── evaluation/
│   ├── rubric.py        weighted dimensions, per-dimension floors, pluggable scorer
│   ├── compliance.py    hard floors: forbidden substrings, link/length/marker rules
│   ├── dedup.py         exact-hash plus shingle similarity
│   └── gate.py          composes the three, in short-circuit order
├── channels/
│   ├── base.py          the four-verb adapter interface, and the transport seam
│   ├── transport.py     DryRunTransport (default) and an unimplemented LiveTransport
│   ├── adapters.py      long-form and short-form examples, each normalizing its own way
│   └── registry.py      config-driven construction; unknown kind is a hard error
├── approval/
│   ├── errors.py        one exception base, so one `except` catches every refusal
│   ├── tokens.py        content-bound HMAC approvals, domain-separated
│   └── ledger.py        single-use enforcement; SQLite INSERT is the test-and-set
├── learning/
│   └── ranker.py        per-dimension ranking, three honest no-winner states, Tilt
└── paid/
    ├── campaign.py      config schema, guardrails, pre-registered stop clock
    ├── intent.py        the deterministic intent digest and the review card
    ├── creative.py      path resolution that cannot depend on the working directory
    ├── platform.py      the transport seam; born-paused; no activation path
    ├── spend_gate.py    signature plus live-state re-check; mints a SpendGrant
    └── collector.py     append-only snapshots, read-time restatement resolution
```

---

## Distilled versus illustrative

This matters more than the code, so it is stated plainly.

This repository is a **clean-room reimplementation** of the mechanisms behind a private
production system that publishes on a schedule and runs paid campaigns. It shares no code,
no configuration, no content, and no account with that system. It was written from an
architectural description, not copied.

### Distilled from a system that runs in production

These mechanisms, and the reasoning behind each, come from a working system. The bugs the
comments describe are bugs that actually happened.

- The content-bound approval token, and the ordering of its checks.
- The spend gate's re-check of every bound field against live platform state, and reporting
  all mismatches at once.
- The deterministic intent digest as a double-spend control, and its exclusion of
  machine-specific paths.
- Born-paused campaigns with no activation path in code.
- Append-only metric snapshots with read-time restatement resolution, and `None` never
  becoming `0`.
- Creative path resolution that cannot depend on the process working directory, and
  re-hashing every creative before the first write.
- The quality gate's short-circuit ordering, and non-compensable floors.
- Two-layer dedup, and slot-derived identity for retry idempotency.
- Per-channel normalization, and the impression floor.
- The three no-winner states, and dropping unmeasured samples.
- The coprime exploration schedule.
- `posting` as a committed pre-network phase, and indeterminate-is-not-failed.
- The two-latch human gate, and auto skipping the tap but never the check.

### Illustrative scaffolding, written for this repository

- **The rubric's ten dimensions** are generic marketing-content dimensions written for this
  repo. The production rubric is private. The *structure* (weights, floors, threshold,
  config-loaded) is the distilled part.
- **The forbidden-terms list** is two obvious placeholders. The real list is the sensitive
  artifact in its config. The *mechanism* is the distilled part.
- **All content is fabricated.** The fact bank, topics, and sample creatives describe an
  invented product and assert nothing about any real company, person, or result.
- **The two channel adapters** are examples chosen to have genuinely different economics.
  They implement the real interface but talk to no real API.
- **The paid side is a sequence, not an autonomous loop.** `OrganicLoop` runs rounds on its
  own; the paid path is driven step by step by `ocm paid` (intent, review card, gate,
  collection). Results collection is implemented and real; deciding what the *next* campaign
  should be from those results is an operator judgment this repository does not automate, and
  the dotted arrow in the diagram says so.
- **The human review surface** (whatever a person actually looks at and clicks) is not here.
  The token that surface would produce is, along with every check performed on it.

### What is stubbed, and what that means

| Piece | Status |
|---|---|
| Transports (organic and paid) | **Stubbed.** `DryRunTransport` and `DryRunPlatform` record calls and return synthetic responses. `LiveTransport.send` raises `NotImplementedError`. |
| The rubric scorer | **Stubbed.** In production a language model reads each dimension's description and grades the text. `stub_scorer` returns a fixed passing value. |
| The content generator | **Stubbed.** `TemplateGenerator` is a deterministic offline template. Every string it writes says so in its own text. |
| Persistence | **Mostly in-memory.** `SqliteLedger` is durable and `PostLog` writes fsynced JSONL when given a path. `SnapshotStore` and the loop's pending-draft map are in-memory only: a restart loses them. |

Two notes on honesty, since a stub is exactly where a demo can mislead:

The rubric scorer returns a **constant**, deliberately not a pseudo-random score. A
pseudo-random score looks like a judgment and is not one: it would trip real compliance
floors at random and print "factual grounding below floor" about text nothing ever assessed.
A constant says plainly that no quality judgment was made. Everything *around* the score is
real and tested: the weighting, the floors, the threshold, and the short-circuit ordering.

The dry-run is **not a mode that could be flipped**. There is no `--live` flag, no command
reads a platform credential, and `LiveTransport.send` raises. `tests/test_dryrun_truth.py`
asserts mechanically that no module in `src/` imports `requests`, `httpx`, `urllib.request`,
`socket`, or `http.client`, that every record the loop produces is marked `dry_run`, and that
the platform stub refuses any status other than `PAUSED` or `ARCHIVED`.

Stated precisely, because the difference matters: the claim is that **nothing in this
repository reaches a network**, not that no future code could. The transport seam exists so a
real client can be injected, which is the point of the design. What is guaranteed is that
none ships here, none is reachable from any `ocm` command, and the test suite fails if one is
added. The one environment variable read anywhere is `OCM_APPROVAL_KEY`, which is this
system's own signing key and not a credential for any external service.

### Scope boundary: single process, single operator

This is a reference implementation of the mechanisms, not a distributed system, and the line
matters because several of the guarantees above are stated at process scope:

- **Concurrency.** The JSONL post log has no atomic reservation, so two processes running the
  same channel could both write an intent and both publish. Single-use approval enforcement
  *is* atomic when backed by `SqliteLedger` (the INSERT is the test-and-set), but the publish
  path is not. A multi-worker deployment needs a transactional store with a unique
  `(channel, draft_id)` reservation.
- **Durability.** `PostLog` persists when given a path. `SnapshotStore`, the pending-draft
  map, and `InMemoryLedger` do not: a restart loses them. The CLI runs fully in memory.
- **A Python object is not a security boundary.** `SpendGrant` and the dry-run platform's
  refusals stop mistakes, not a determined caller in the same process. Real enforcement
  belongs server-side, at the provider, with an idempotency key.
- **The gate binds content, not identity.** HMAC proves a token was made by someone holding
  the key. It does not prove *which* person approved, and this repository has no identity
  system. A setting where the approver must be provably distinct from the spender needs an
  asymmetric signature with the private key held only by the approver.

### Not claimed

Engagement rank is **not causal lift**. Nothing here runs a holdout, so nothing here can
attribute a result to a creative. The guidance strings say "scored higher", never "performed
better because". Measuring real incrementality is a different and much more expensive
instrument than this loop has.

---

## Configuration

Every value an operator might reasonably want to change lives in `config/example/`: the
rubric and its thresholds, the compliance rules, the style space, the channel list and each
channel's limits, the learning parameters, the schedule policy, and every paid guardrail.
Changing any of them is a config edit, never a code change.

Code-level fallbacks do exist for the operational knobs (retry budget, similarity threshold,
window length), so a partial config still runs. The paid guardrails are the deliberate
exception: budget, currency, and `decision_metric` have **no** default and a campaign missing
any of them fails to load. A money ceiling that quietly defaults is not a ceiling.

**Credentials never live in config.** `resolve_env_ref` accepts only an environment
reference of the form `${UPPER_SNAKE}` and *refuses* a literal, because a warning gets
scrolled past and a secret in a config file gets committed. It is the helper every credential
field must be read through; the loader does not scan arbitrary config for secret-looking
values, since it cannot know which of an operator's fields are meant to be credentials. This
repository ships no key, no `.env`, and no config field that needs one.

---

## Tests

740 tests, covering the mechanisms above rather than chasing line coverage: the gate's
short-circuit (proved with a scorer that raises if called) and its non-compensable floors,
both dedup layers, every approval refusal path plus the ordering guarantee that a rejected
token is not burned, domain separation, the spend gate's live re-check and its
all-reasons-at-once refusal, digest determinism and machine independence,
working-directory-independent creative resolution, restatement resolution and
missing-is-not-zero, the transition table, the durable intent row and its survival across a
reload, the three no-winner states and that all three are reachable, absent-is-not-zero, the
property test for exploration coverage, the full human-approval path including replay and
wrong-key refusal, and the honesty tests that mechanically prove the dry-run claims.

A number of them are regression guards for bugs found while building this, each of which had
been invisible in ordinary use: a schedule cap that never fired under an injected clock, an
inclusive window boundary that would have halved a daily cadence, an exploration stride whose
coprimality test never actually constrained anything, a spend gate that burned a valid human
approval on a transient mismatch, an intent digest whose separator could be forged into a
collision, and a forbidden-term check that a single left-to-right mark walked straight past.

```bash
.venv/bin/python -m pytest -v
```

---

## License

MIT. See [LICENSE](LICENSE).
