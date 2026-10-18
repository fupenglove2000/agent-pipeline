import httpx
import openai
import pytest
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from agent_pipeline.graph.failures import (
    FailureCategory,
    UnclassifiedFailureError,
    classify_failure,
    is_transient,
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
        (_status_error(openai.InternalServerError, 500), FailureCategory.TRANSIENT),
        (_status_error(openai.InternalServerError, 503), FailureCategory.TRANSIENT),
        (openai.APITimeoutError(request=REQUEST), FailureCategory.TRANSIENT),
        (openai.APIConnectionError(request=REQUEST), FailureCategory.TRANSIENT),
        (_validation_error(), FailureCategory.MALFORMED_OUTPUT),
        (_status_error(openai.AuthenticationError, 401), FailureCategory.SYSTEMIC),
        (
            OperationalError("SELECT 1", {}, Exception("connection refused")),
            FailureCategory.SYSTEMIC,
        ),
    ],
    ids=[
        "rate-limit",
        "server-500",
        "server-503",
        "timeout",
        "connection",
        "validation",
        "auth",
        "db-unreachable",
    ],
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


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_status_error(openai.RateLimitError, 429), True),
        (_status_error(openai.InternalServerError, 503), True),
        (openai.APITimeoutError(request=REQUEST), True),
        (openai.APIConnectionError(request=REQUEST), True),
        (_status_error(openai.AuthenticationError, 401), False),
        (_validation_error(), False),
        (RuntimeError("never seen"), False),
    ],
    ids=["rate-limit", "server-503", "timeout", "connection", "auth", "validation", "unknown"],
)
def test_is_transient_answers_without_raising(exc: Exception, expected: bool) -> None:
    # Used as the graph's retry_on: it must give a yes/no for anything, including
    # exceptions classify_failure would refuse to classify.
    assert is_transient(exc) is expected


@pytest.mark.parametrize(
    "exc",
    [
        _status_error(openai.APIStatusError, 408),
        _status_error(openai.ConflictError, 409),
    ],
    ids=["408", "409"],
)
def test_408_and_409_are_deliberately_left_unclassified(exc: Exception) -> None:
    # The SDK used to retry these, but they are rare enough that the table waits
    # for a real occurrence before adding a row (see failures.py).
    assert is_transient(exc) is False
    with pytest.raises(UnclassifiedFailureError):
        classify_failure(exc)
