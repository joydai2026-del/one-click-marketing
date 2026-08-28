"""The approval exception hierarchy, in its own module so both halves can share it.

`ledger` and `tokens` both raise refusals, and `tokens` imports `ledger`, so the base
class cannot live in either without a cycle. It lives here.

Every refusal in this package derives from `ApprovalError`. That is deliberate and is the
contract callers rely on: a caller wrapping the gate can write one `except ApprovalError`
and be certain it has caught every reason the system said no. A refusal that escaped that
base class would be a refusal some caller silently treats as a crash, and a crash in a
publish path is frequently retried.
"""

from __future__ import annotations


class ApprovalError(Exception):
    """Base class. Any subclass means: do not proceed, under any circumstances."""


class SignatureError(ApprovalError):
    """Token was not signed by the expected key, or was tampered with."""


class ExpiredError(ApprovalError):
    """Token is past its expiry."""


class ScopeMismatchError(ApprovalError):
    """Token authorizes a different action than the one being attempted."""


class ContentMismatchError(ApprovalError):
    """Token was issued for different content than what is about to be sent."""


class SpendLimitError(ApprovalError):
    """The attempted spend exceeds the amount the human approved."""


class ReplayError(ApprovalError):
    """A nonce that has already been consumed was presented again."""
