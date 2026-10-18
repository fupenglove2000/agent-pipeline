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


def classify_failure(exc: Exception) -> FailureCategory:
    # APITimeoutError subclasses APIConnectionError; both are listed for readability.
    if isinstance(exc, openai.RateLimitError | openai.APITimeoutError | openai.APIConnectionError):
        return FailureCategory.TRANSIENT
    if isinstance(exc, ValidationError):
        return FailureCategory.MALFORMED_OUTPUT
    if isinstance(exc, openai.AuthenticationError | OperationalError):
        return FailureCategory.SYSTEMIC
    raise UnclassifiedFailureError(f"no ADR-0003 category for {type(exc).__name__}: {exc}") from exc
