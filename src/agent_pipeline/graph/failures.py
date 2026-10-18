"""Failure classification per ADR-0003's fixed table.

Maps an exception to one of the table's four categories. Only the exception
types the table names are covered; anything else raises
`UnclassifiedFailureError` rather than being given a category. ADR-0003: a
failure that fits no row is a bug in the table to be fixed with a conscious
decision, "not a case to force into the nearest category". Returning an
UNCLASSIFIED marker instead would let callers fall through to a default branch,
which is exactly the silent default the ADR rules out; raising cannot be ignored.

Known gaps, kept visible on purpose:
- NON_RETRYABLE (content-policy refusal, safety block) has no exception type
  mapped to it yet. How the OpenAI SDK surfaces a refusal (finish_reason, a
  `refusal` field, or an exception) is undecided, so the first real one will
  raise UnclassifiedFailureError — which is the intended signal to extend this.
- `OperationalError` stands in for "DB unreachable" but is wider than that
  (deadlocks and serialisation failures are OperationalErrors too). Narrow it
  when the runner issues transactions where those can occur.
"""

import enum

import openai
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError


class FailureCategory(enum.Enum):
    TRANSIENT = "TRANSIENT"
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    NON_RETRYABLE = "NON_RETRYABLE"
    SYSTEMIC = "SYSTEMIC"


class UnclassifiedFailureError(Exception):
    """An exception ADR-0003's table has no row for."""


# The SDK's own retries are off (see llm.py), so the transient cases it used to
# retry are this table's to cover. APITimeoutError subclasses APIConnectionError;
# both are listed for readability. InternalServerError is every 5xx.
#
# Deliberately not covered: 408 and 409, which the SDK also retried. Both are
# rare for chat completions, and 408 has no class of its own (it arrives as the
# APIStatusError base), so covering it means matching the base class plus a status
# check. Until one is actually seen, letting it surface as UnclassifiedFailureError
# is the ADR-0003 way to find out it matters, and then add a row on purpose.
_TRANSIENT_ERRORS = (
    openai.RateLimitError,
    openai.InternalServerError,
    openai.APITimeoutError,
    openai.APIConnectionError,
)


def is_transient(exc: Exception) -> bool:
    """Retry predicate for the graph's RetryPolicy. Unlike classify_failure it never
    raises: an unknown exception is simply not retried, so it reaches run_task
    unchanged and is turned into UnclassifiedFailureError there."""
    return isinstance(exc, _TRANSIENT_ERRORS)


def classify_failure(exc: Exception) -> FailureCategory:
    if is_transient(exc):
        return FailureCategory.TRANSIENT
    if isinstance(exc, ValidationError):
        return FailureCategory.MALFORMED_OUTPUT
    if isinstance(exc, openai.AuthenticationError | OperationalError):
        return FailureCategory.SYSTEMIC
    raise UnclassifiedFailureError(f"no ADR-0003 category for {type(exc).__name__}: {exc}") from exc
