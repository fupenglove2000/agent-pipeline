import httpx
import openai
import pytest
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from agent_pipeline.graph.failures import (
    FailureCategory,
    UnclassifiedFailureError,
    classify_failure,
)
from agent_pipeline.graph.state import ParsedResult

REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _status_error(cls: type[openai.APIStatusError], status_code: int) -> openai.APIStatusError:
    return cls("boom", response=httpx.Response(status_code, request=REQUEST), body=None)


def _validation_error() -> ValidationError:
    with pytest.raises(ValidationError) as exc_info:
        ParsedResult.model_validate_json("not json")
    return exc_info.value


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_status_error(openai.RateLimitError, 429), FailureCategory.TRANSIENT),
        (openai.APITimeoutError(request=REQUEST), FailureCategory.TRANSIENT),
        (openai.APIConnectionError(request=REQUEST), FailureCategory.TRANSIENT),
        (_validation_error(), FailureCategory.MALFORMED_OUTPUT),
        (_status_error(openai.AuthenticationError, 401), FailureCategory.SYSTEMIC),
        (
            OperationalError("SELECT 1", {}, Exception("connection refused")),
            FailureCategory.SYSTEMIC,
        ),
    ],
    ids=["rate-limit", "timeout", "connection", "validation", "auth", "db-unreachable"],
)
def test_classifies_every_exception_type_named_in_adr_0003(
    exc: Exception, expected: FailureCategory
) -> None:
    assert classify_failure(exc) is expected


def test_unknown_exception_is_raised_not_guessed() -> None:
    # ADR-0003: a failure outside the table is a bug in the table, not something
    # to force into the nearest category. Raising (instead of returning an
    # "unclassified" marker) means no caller can fall through to a default.
    class NeverSeenBefore(Exception):
        pass

    original = NeverSeenBefore("surprise")

    with pytest.raises(UnclassifiedFailureError) as exc_info:
        classify_failure(original)

    assert exc_info.value.__cause__ is original


def test_a_related_but_unlisted_openai_error_is_not_guessed() -> None:
    # BadRequestError is an APIStatusError like the listed ones, but the table has
    # no row for it — sharing a base class is not enough to be classified.
    with pytest.raises(UnclassifiedFailureError):
        classify_failure(_status_error(openai.BadRequestError, 400))
