"""Human approval that is bound to specific content, not to a vague intent.

Every refusal in this package derives from `ApprovalError`, so one `except ApprovalError`
catches every reason the system said no. See `errors.py` for why that matters.
"""

from .errors import (
    ApprovalError,
    ContentMismatchError,
    ExpiredError,
    ReplayError,
    ScopeMismatchError,
    SignatureError,
    SpendLimitError,
)
from .ledger import InMemoryLedger, NonceLedger, SqliteLedger
from .tokens import (
    ApprovalToken,
    ephemeral_key,
    issue,
    require_env_key,
    verify,
    verify_and_consume,
)

__all__ = [
    "ApprovalError",
    "SignatureError",
    "ExpiredError",
    "ScopeMismatchError",
    "ContentMismatchError",
    "SpendLimitError",
    "ReplayError",
    "NonceLedger",
    "InMemoryLedger",
    "SqliteLedger",
    "ApprovalToken",
    "issue",
    "verify",
    "verify_and_consume",
    "ephemeral_key",
    "require_env_key",
]
