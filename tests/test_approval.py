"""Content-bound approval tokens: every refusal, and the ORDER the refusals happen in.

"A human approved it" is worth very little if the thing approved can change afterwards, or
if the approval can be replayed, or if a publish approval can be spent.
"""

from __future__ import annotations

import dataclasses

import pytest

from ocm.approval.errors import (
    ApprovalError,
    ContentMismatchError,
    ExpiredError,
    ReplayError,
    ScopeMismatchError,
    SignatureError,
    SpendLimitError,
)
from ocm.approval.ledger import InMemoryLedger, SqliteLedger
from ocm.approval.tokens import (
    DOMAIN_PUBLISH,
    DOMAIN_SPEND,
    ApprovalToken,
    _sign,
    domain_for,
    ephemeral_key,
    issue,
    signing_input,
    verify_and_consume,
)

KEY = b"a-fixed-test-key-that-is-not-a-real-secret"
OTHER_KEY = b"a-different-fixed-test-key-also-not-a-secret"
HASH = "c" * 64
T0 = 1_800_000_000.0


@pytest.fixture
def ledger() -> InMemoryLedger:
    return InMemoryLedger()


def publish_token(**overrides):
    kwargs = dict(
        scope="publish:substack",
        content_hash=HASH,
        subject="r0-substack-abc",
        key=KEY,
        now=T0,
        ttl_seconds=3600,
    )
    kwargs.update(overrides)
    return issue(**kwargs)


def verify(token, sig, *, ledger, **overrides):
    kwargs = dict(
        token=token,
        signature=sig,
        key=KEY,
        expected_scope=token.scope,
        expected_content_hash=token.content_hash,
        ledger=ledger,
        now=T0 + 1,
    )
    kwargs.update(overrides)
    return verify_and_consume(**kwargs)


# --------------------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------------------


def test_happy_path_issue_then_verify_and_consume(ledger):
    token, sig = publish_token()
    verified = verify(token, sig, ledger=ledger)
    assert verified is token
    assert ledger.seen(token.nonce) is True


def test_issue_binds_the_token_to_the_content_hash_and_the_clock(ledger):
    token, _ = publish_token()
    assert token.content_hash == HASH
    assert token.issued_at == T0
    assert token.expires_at == T0 + 3600
    assert token.approver == "operator"


def test_two_tokens_for_the_same_content_carry_different_nonces():
    """Single-use is per approval, not per piece of content."""
    a, _ = publish_token()
    b, _ = publish_token()
    assert a.nonce != b.nonce


# --------------------------------------------------------------------------------------
# the five refusals
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("content_hash", "d" * 64),
        ("subject", "some-other-draft"),
        ("expires_at", T0 + 10_000_000),
        ("max_spend_minor", 999_999),
        ("approver", "someone-else"),
        ("issued_at", T0 - 10_000),
    ],
)
def test_a_tampered_payload_field_fails_the_signature(ledger, field, value):
    """Every field is attacker-controlled until the signature says otherwise, so ALL of
    them must be inside the MAC, not just the obviously sensitive ones."""
    token, sig = publish_token()
    tampered = dataclasses.replace(token, **{field: value})

    with pytest.raises(SignatureError):
        verify(
            tampered,
            sig,
            ledger=ledger,
            expected_scope=token.scope,
            expected_content_hash=token.content_hash,
        )


def test_a_token_signed_with_the_wrong_key_is_refused(ledger):
    token, sig = publish_token()
    with pytest.raises(SignatureError):
        verify(token, sig, ledger=ledger, key=OTHER_KEY)


def test_a_garbage_signature_is_refused_without_raising_something_else(ledger):
    token, _ = publish_token()
    with pytest.raises(SignatureError):
        verify(token, "not-a-signature", ledger=ledger)


def test_an_expired_token_is_refused(ledger):
    token, sig = publish_token(ttl_seconds=60)
    with pytest.raises(ExpiredError):
        verify(token, sig, ledger=ledger, now=T0 + 61)


def test_expiry_is_inclusive_at_the_boundary(ledger):
    """`t >= expires_at` means the token is dead ON its expiry second, not one after."""
    token, sig = publish_token(ttl_seconds=60)
    with pytest.raises(ExpiredError):
        verify(token, sig, ledger=ledger, now=T0 + 60)
    assert verify(token, sig, ledger=ledger, now=T0 + 59.9)


def test_a_publish_token_presented_for_a_spend_is_refused(ledger):
    """The scope check: an approval to post must never authorize money to move."""
    token, sig = publish_token()
    with pytest.raises(ScopeMismatchError):
        verify(token, sig, ledger=ledger, expected_scope="spend:camp-1")


def test_a_publish_token_for_one_channel_does_not_authorize_another(ledger):
    token, sig = publish_token(scope="publish:substack")
    with pytest.raises(ScopeMismatchError):
        verify(token, sig, ledger=ledger, expected_scope="publish:x")


def test_content_that_changed_after_approval_is_refused(ledger):
    """The whole point: the approval cannot float free of what was reviewed."""
    token, sig = publish_token()
    with pytest.raises(ContentMismatchError):
        verify(token, sig, ledger=ledger, expected_content_hash="d" * 64)


def test_replaying_the_same_approval_is_refused(ledger):
    """Without a ledger a valid token is a reusable coupon."""
    token, sig = publish_token()
    verify(token, sig, ledger=ledger)
    with pytest.raises(ReplayError):
        verify(token, sig, ledger=ledger)


# --------------------------------------------------------------------------------------
# ORDERING: a refused token must not be burned
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,overrides",
    [
        ("expired", {"now": T0 + 100_000}),
        ("misscoped", {"expected_scope": "spend:camp-1"}),
        ("content mismatch", {"expected_content_hash": "d" * 64}),
    ],
)
def test_a_token_refused_for_a_non_signature_reason_does_not_burn_its_nonce(
    ledger, kind, overrides
):
    """The nonce is consumed LAST, after every other check.

    Burning it earlier would mean a clock skew, a mis-typed scope, or a stale expectation
    permanently destroys a valid human approval, and the operator has to go get another
    one for content that never changed.
    """
    token, sig = publish_token(ttl_seconds=3600)

    with pytest.raises(ApprovalError):
        verify(token, sig, ledger=ledger, **overrides)

    assert ledger.seen(token.nonce) is False, f"{kind} burned the nonce"

    # And the approval is still usable once the caller presents it correctly.
    assert verify(token, sig, ledger=ledger) is token
    assert ledger.seen(token.nonce) is True


def test_a_bad_signature_does_not_burn_the_nonce_either(ledger):
    token, sig = publish_token()
    with pytest.raises(SignatureError):
        verify(token, sig, ledger=ledger, key=OTHER_KEY)
    assert ledger.seen(token.nonce) is False


def test_a_spend_ceiling_breach_does_not_burn_the_nonce(ledger):
    token, sig = issue(
        scope="spend:camp-1", content_hash=HASH, subject="camp-1", key=KEY,
        max_spend_minor=1000, now=T0,
    )
    with pytest.raises(SpendLimitError):
        verify(token, sig, ledger=ledger, intended_spend_minor=5000)
    assert ledger.seen(token.nonce) is False


# --------------------------------------------------------------------------------------
# the spend ceiling
# --------------------------------------------------------------------------------------


def test_an_intended_spend_over_the_approved_ceiling_is_refused(ledger):
    token, sig = issue(
        scope="spend:camp-1", content_hash=HASH, subject="camp-1", key=KEY,
        max_spend_minor=25_000, now=T0,
    )
    with pytest.raises(SpendLimitError, match="exceeds approved ceiling"):
        verify(token, sig, ledger=ledger, intended_spend_minor=25_001)


def test_an_intended_spend_exactly_at_the_ceiling_is_allowed(ledger):
    token, sig = issue(
        scope="spend:camp-1", content_hash=HASH, subject="camp-1", key=KEY,
        max_spend_minor=25_000, now=T0,
    )
    assert verify(token, sig, ledger=ledger, intended_spend_minor=25_000)


def test_a_token_with_no_ceiling_authorizes_no_spend_at_all(ledger):
    """None means "no spend", not "unlimited". Defaulting the other way would make every
    publish approval a blank cheque."""
    token, sig = issue(
        scope="spend:camp-1", content_hash=HASH, subject="camp-1", key=KEY,
        max_spend_minor=None, now=T0,
    )
    with pytest.raises(SpendLimitError, match="authorizes no spend"):
        verify(token, sig, ledger=ledger, intended_spend_minor=1)


def test_the_ceiling_is_not_checked_when_no_spend_is_intended(ledger):
    """A publish path passes no intended spend, so a ceiling-less token is fine there."""
    token, sig = publish_token()
    assert verify(token, sig, ledger=ledger, intended_spend_minor=None)


# --------------------------------------------------------------------------------------
# domain separation
# --------------------------------------------------------------------------------------


def test_the_two_domains_are_distinct_and_neither_is_a_prefix_of_the_other():
    """NUL termination is what makes the separation total rather than merely likely."""
    assert DOMAIN_PUBLISH != DOMAIN_SPEND
    assert not DOMAIN_PUBLISH.startswith(DOMAIN_SPEND)
    assert not DOMAIN_SPEND.startswith(DOMAIN_PUBLISH)
    assert DOMAIN_PUBLISH.endswith(b"\x00") and DOMAIN_SPEND.endswith(b"\x00")


def test_domain_for_maps_each_scope_family_and_refuses_an_unknown_one():
    assert domain_for("publish:substack") == DOMAIN_PUBLISH
    assert domain_for("spend:camp-1") == DOMAIN_SPEND
    with pytest.raises(ApprovalError, match="unknown approval scope family"):
        domain_for("delete:everything")


def test_the_domain_prefix_is_actually_inside_the_signed_bytes(ledger):
    """A signature computed over the SPEND payload but with the PUBLISH domain prefix must
    not verify. Without this the prefix could be applied on one side and forgotten on the
    other, and the separation would exist only in the docstring.
    """
    spend_token = ApprovalToken(
        scope="spend:camp-1",
        content_hash=HASH,
        subject="camp-1",
        issued_at=T0,
        expires_at=T0 + 3600,
        nonce="fixed-nonce-for-this-test",
        max_spend_minor=25_000,
    )
    wrong_domain_sig = _sign(DOMAIN_PUBLISH + spend_token.canonical(), KEY)

    with pytest.raises(SignatureError):
        verify_and_consume(
            token=spend_token,
            signature=wrong_domain_sig,
            key=KEY,
            expected_scope="spend:camp-1",
            expected_content_hash=HASH,
            ledger=ledger,
            intended_spend_minor=1,
            now=T0 + 1,
        )
    assert ledger.seen(spend_token.nonce) is False

    # The correctly domained signature over the identical payload does verify, so the only
    # difference between the two cases is the domain prefix.
    right = _sign(signing_input(spend_token), KEY)
    assert verify_and_consume(
        token=spend_token,
        signature=right,
        key=KEY,
        expected_scope="spend:camp-1",
        expected_content_hash=HASH,
        ledger=ledger,
        intended_spend_minor=1,
        now=T0 + 1,
    )


def test_signing_input_starts_with_the_domain_for_the_tokens_scope():
    token, _ = publish_token()
    assert signing_input(token).startswith(DOMAIN_PUBLISH)


def test_issue_refuses_a_scope_family_with_no_domain():
    with pytest.raises(ApprovalError):
        issue(scope="archive:everything", content_hash=HASH, subject="s", key=KEY, now=T0)


# --------------------------------------------------------------------------------------
# issue-time validation
# --------------------------------------------------------------------------------------


def test_issue_refuses_an_empty_content_hash():
    """An approval bound to nothing is an approval that authorizes everything."""
    with pytest.raises(ValueError, match="not bound to any content"):
        issue(scope="publish:x", content_hash="", subject="s", key=KEY, now=T0)


@pytest.mark.parametrize("ttl", [0, -1, -3600])
def test_issue_refuses_a_non_positive_ttl(ttl):
    """A zero or negative ttl mints a token that is already dead, so the caller silently
    gets a refusal at spend time instead of an error at approval time."""
    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        issue(scope="publish:x", content_hash=HASH, subject="s", key=KEY, ttl_seconds=ttl, now=T0)


def test_canonical_bytes_are_deterministic_and_sorted():
    token, _ = publish_token()
    assert token.canonical() == token.canonical()
    assert b'"content_hash"' in token.canonical()
    # Sorted keys: approver sorts before content_hash sorts before expires_at.
    raw = token.canonical().decode()
    assert raw.index('"approver"') < raw.index('"content_hash"') < raw.index('"expires_at"')


def test_ephemeral_keys_differ_between_calls():
    assert ephemeral_key() != ephemeral_key()
    assert len(ephemeral_key()) == 32


# --------------------------------------------------------------------------------------
# the one-except contract
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls",
    [
        SignatureError,
        ExpiredError,
        ScopeMismatchError,
        ContentMismatchError,
        SpendLimitError,
        ReplayError,
    ],
)
def test_every_refusal_derives_from_approval_error(cls):
    """One `except ApprovalError` must catch every reason the system said no.

    A refusal escaping that base class is a refusal some caller treats as a crash, and a
    crash in a publish path is frequently retried.
    """
    assert issubclass(cls, ApprovalError)
    assert issubclass(cls, Exception)


def test_a_caller_can_catch_every_refusal_with_one_except(ledger):
    token, sig = publish_token()
    for overrides in (
        {"key": OTHER_KEY},
        {"now": T0 + 100_000},
        {"expected_scope": "spend:c"},
        {"expected_content_hash": "d" * 64},
    ):
        try:
            verify(token, sig, ledger=ledger, **overrides)
        except ApprovalError:
            pass
        else:  # pragma: no cover - the test fails loudly if a refusal escapes
            raise AssertionError(f"{overrides} was not refused")


# --------------------------------------------------------------------------------------
# ledgers
# --------------------------------------------------------------------------------------


def test_in_memory_ledger_is_a_test_and_set():
    ledger = InMemoryLedger()
    assert ledger.seen("n1") is False
    ledger.consume("n1")
    assert ledger.seen("n1") is True
    with pytest.raises(ReplayError):
        ledger.consume("n1")


def test_the_replay_message_does_not_print_the_whole_nonce():
    ledger = InMemoryLedger()
    nonce = "a-long-and-recognizable-nonce-value-1234567890"
    ledger.consume(nonce)
    with pytest.raises(ReplayError) as exc:
        ledger.consume(nonce)
    assert nonce not in str(exc.value)


def test_sqlite_ledger_refuses_a_replay_across_a_fresh_connection(tmp_path):
    """Durability is the whole reason this implementation exists: an in-memory ledger
    forgets every consumed approval on restart, so every token becomes replayable exactly
    when the process is least healthy.
    """
    path = str(tmp_path / "nonces.db")

    first = SqliteLedger(path)
    first.consume("nonce-1")
    assert first.seen("nonce-1") is True
    first.close()

    second = SqliteLedger(path)
    try:
        assert second.seen("nonce-1") is True
        with pytest.raises(ReplayError):
            second.consume("nonce-1")
        # An unrelated nonce is still usable, so the refusal is specific and not blanket.
        second.consume("nonce-2")
    finally:
        second.close()


def test_sqlite_ledger_enforces_single_use_end_to_end_through_verify(tmp_path):
    path = str(tmp_path / "nonces.db")
    token, sig = publish_token()

    ledger = SqliteLedger(path)
    try:
        verify(token, sig, ledger=ledger)
    finally:
        ledger.close()

    reopened = SqliteLedger(path)
    try:
        with pytest.raises(ReplayError):
            verify(token, sig, ledger=reopened)
    finally:
        reopened.close()
