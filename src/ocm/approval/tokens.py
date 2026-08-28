"""Content-bound approval tokens.

THE PROBLEM THIS SOLVES

"A human approved it" is worth very little if the thing approved can change afterwards.
The failure mode in an automated marketing loop is mundane and expensive: an operator
reviews draft A, clicks approve, and by the time the publisher or the ad platform runs,
the pipeline has regenerated the creative into draft B. The approval is still "valid" and
now authorizes something no human ever saw. Money moves against unreviewed content.

THE MECHANISM

An approval token is an HMAC-SHA256 over a canonical, sorted payload:

    {scope, content_hash, subject, issued_at, expires_at, nonce, max_spend_minor}

`content_hash` is the fingerprint of the exact bytes that were shown to the human. At
publish or spend time the caller recomputes the hash of what it is ABOUT to send and
passes it to `verify_and_consume`. If a single character of the creative changed, the
hashes differ and the token is refused. The approval cannot float free of its content.

Four independent checks, each rejecting a different attack:

    signature    forged or tampered token          -> SignatureError
    expiry       stale approval used months later  -> ExpiredError
    scope        publish approval reused to spend  -> ScopeMismatchError
    content      approved A, about to send B       -> ContentMismatchError
    nonce        same approval used twice          -> ReplayError (from the ledger)

Order matters: the signature is checked FIRST, with `hmac.compare_digest`, before any
field of the token is trusted or logged. Every other field is attacker-controlled until
the signature says otherwise.

The nonce is consumed LAST, only after every other check has passed, so that a token
rejected for being expired or misscoped is not silently burned.

KEY HANDLING

The signing key is read from the environment (`OCM_APPROVAL_KEY`) and never written to
disk, never logged, and never placed in a config file. This repository ships no key. The
dry-run generates an ephemeral in-process key so the demo runs with no setup; that key
dies with the process, which is exactly what you want from a demo and exactly what you
must not do in production. `require_env_key()` is the production path.

WHAT THIS IS NOT

HMAC gives integrity and authenticity under a shared secret. It does not give
non-repudiation: anyone holding the key can mint a token, so the issuer and the verifier
cannot cryptographically distinguish each other. That is the right trade for a
single-operator system where the same process both asks and enforces. A multi-party
setting where the approver must be provably distinct from the spender wants an asymmetric
signature (Ed25519) with the private key held only by the approver, and the verify path
below is deliberately shaped so that swapping the primitive touches `_sign` and `_verify`
only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any

from .errors import (
    ApprovalError,
    ContentMismatchError,
    ExpiredError,
    ScopeMismatchError,
    SignatureError,
    SpendLimitError,
)
from .ledger import NonceLedger

ENV_KEY = "OCM_APPROVAL_KEY"
DEFAULT_TTL_SECONDS = 3600

# DOMAIN SEPARATION
#
# One key signs more than one kind of artifact. Without a domain prefix, an attacker who
# can get one artifact type signed may be able to present those same bytes as the other
# type. The prefix is NUL-terminated so that no domain string can be a prefix of another,
# which is what makes the separation actually total rather than merely likely.
#
# `signing_input` is used by BOTH issue and verify, so the prefix cannot be applied on one
# side and forgotten on the other.
DOMAIN_PUBLISH = b"ocm.approval.publish.v1\x00"
DOMAIN_SPEND = b"ocm.approval.spend.v1\x00"


def domain_for(scope: str) -> bytes:
    """Map a scope onto its signing domain. An unknown scope family is refused.

    Refused rather than defaulted: a default domain would silently place a novel scope in
    the same signing namespace as an existing one, which is the exact confusion the
    domain prefix exists to prevent.
    """
    family = scope.split(":", 1)[0]
    if family == "publish":
        return DOMAIN_PUBLISH
    if family == "spend":
        return DOMAIN_SPEND
    raise ApprovalError(f"unknown approval scope family {family!r}")


def signing_input(token: ApprovalToken) -> bytes:
    """The exact bytes that get signed: domain prefix followed by the canonical payload."""
    return domain_for(token.scope) + token.canonical()


@dataclass(frozen=True)
class ApprovalToken:
    """The payload a human's approval produces. Serialized alongside its signature."""

    scope: str  # e.g. "publish:substack" or "spend:campaign"
    content_hash: str  # fingerprint of exactly what the human saw
    subject: str  # the draft id, campaign id, or creative id
    issued_at: float
    expires_at: float
    nonce: str
    approver: str = "operator"
    # For paid scopes: the ceiling the human agreed to, in minor units (e.g. cents).
    # None means the token authorizes no spend at all.
    max_spend_minor: int | None = None

    def canonical(self) -> bytes:
        """Deterministic bytes to sign. Sorted keys, no whitespace, explicit encoding."""
        # allow_nan=False: NaN and Infinity are not valid JSON, and a NaN timestamp would
        # make every expiry comparison false, so an expired token would never expire.
        return json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _key() -> bytes:
    k = os.environ.get(ENV_KEY)
    if not k:
        raise ApprovalError(
            f"{ENV_KEY} is not set. Approval signing requires a key; refusing to proceed."
        )
    return k.encode("utf-8")


def require_env_key() -> bytes:
    """Production path: fail loudly rather than fall back to an ephemeral key."""
    return _key()


def ephemeral_key() -> bytes:
    """Dry-run only. A fresh random key that dies with the process."""
    return secrets.token_bytes(32)


def _sign(payload: bytes, key: bytes) -> str:
    mac = hmac.new(key, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def _verify(payload: bytes, signature: str, key: bytes) -> bool:
    expected = _sign(payload, key)
    # Constant-time: a timing oracle on signature comparison leaks the expected MAC.
    return hmac.compare_digest(expected, signature)


def issue(
    *,
    scope: str,
    content_hash: str,
    subject: str,
    key: bytes,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    approver: str = "operator",
    max_spend_minor: int | None = None,
    now: float | None = None,
) -> tuple[ApprovalToken, str]:
    """Mint a token for content the human has just seen and approved.

    Returns (token, signature). Both must be presented at verification time.
    """
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    if not content_hash:
        raise ValueError("refusing to issue an approval not bound to any content")
    t = now if now is not None else time.time()
    token = ApprovalToken(
        scope=scope,
        content_hash=content_hash,
        subject=subject,
        issued_at=t,
        expires_at=t + ttl_seconds,
        nonce=secrets.token_urlsafe(24),
        approver=approver,
        max_spend_minor=max_spend_minor,
    )
    return token, _sign(signing_input(token), key)


def verify(
    *,
    token: ApprovalToken,
    signature: str,
    key: bytes,
    expected_scope: str,
    expected_content_hash: str,
    expected_subject: str | None = None,
    intended_spend_minor: int | None = None,
    now: float | None = None,
) -> ApprovalToken:
    """Every check EXCEPT consuming the nonce. Raises on any failure.

    Split out from `verify_and_consume` so a caller with further checks of its own (the
    spend gate re-reads live platform state) can validate everything first and burn the
    nonce only once it is actually going to act. Consuming first means a transient
    mismatch destroys a perfectly good approval and a human has to re-issue it.
    """
    # 1. Signature first: nothing in the token is trustworthy until this passes.
    if not _verify(signing_input(token), signature, key):
        raise SignatureError("approval signature is invalid")

    t = now if now is not None else time.time()

    # 2. Expiry.
    if t >= token.expires_at:
        raise ExpiredError(f"approval expired at {token.expires_at:.0f}, now {t:.0f}")

    # 3. Scope: a publish approval must not authorize a spend.
    if token.scope != expected_scope:
        raise ScopeMismatchError(
            f"approval scope {token.scope!r} does not authorize {expected_scope!r}"
        )

    # 4. Content binding: the whole point.
    if not hmac.compare_digest(token.content_hash, expected_content_hash):
        raise ContentMismatchError(
            "content changed after approval: refusing to act on unreviewed content"
        )

    # 5. Subject: WHICH thing was approved, not just what it said.
    #
    #    Content hash and subject are different questions and both must be asked. Two
    #    drafts can carry byte-identical content (the same copy scheduled twice, a repost)
    #    and an approval for one is not an approval for the other.
    if expected_subject is not None and not hmac.compare_digest(
        token.subject, expected_subject
    ):
        raise ContentMismatchError(
            f"approval names subject {token.subject!r}, not {expected_subject!r}"
        )

    # 6. Spend ceiling, for paid scopes.
    if intended_spend_minor is not None:
        if intended_spend_minor <= 0:
            raise SpendLimitError("intended spend must be positive")
        if token.max_spend_minor is None:
            raise SpendLimitError("approval authorizes no spend")
        if intended_spend_minor > token.max_spend_minor:
            raise SpendLimitError(
                f"intended spend {intended_spend_minor} exceeds approved ceiling "
                f"{token.max_spend_minor}"
            )
    return token


def verify_and_consume(
    *,
    token: ApprovalToken,
    signature: str,
    key: bytes,
    expected_scope: str,
    expected_content_hash: str,
    ledger: NonceLedger,
    expected_subject: str | None = None,
    intended_spend_minor: int | None = None,
    now: float | None = None,
) -> ApprovalToken:
    """Verify, then burn the nonce. Raises on any failure.

    The caller must pass `expected_content_hash` computed from the bytes it is ABOUT to
    send, not from the bytes it once stored. Recomputing at the point of action is what
    makes the binding real; reading back the hash the token itself carries would verify
    nothing at all.
    """
    verify(
        token=token,
        signature=signature,
        key=key,
        expected_scope=expected_scope,
        expected_content_hash=expected_content_hash,
        expected_subject=expected_subject,
        intended_spend_minor=intended_spend_minor,
        now=now,
    )
    # 6. Single use. Consumed LAST so a token rejected above is not burned.
    ledger.consume(token.nonce)
    return token
