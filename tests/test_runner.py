from collections.abc import Iterator

import httpx
import openai
import pytest
from sqlalchemy import select
from testcontainers.community.postgres import PostgresContainer

from agent_pipeline import db
from agent_pipeline.config import get_settings
from agent_pipeline.graph.build import MAX_PARSE_ATTEMPTS, TRANSIENT_MAX_ATTEMPTS
from agent_pipeline.graph.failures import FailureCategory, UnclassifiedFailureError
from agent_pipeline.llm import LLMResponse
from agent_pipeline.models import AttemptOutcome, Base, Task, TaskAttempt, TaskStatus
from agent_pipeline.normalize import derive_task_key
from agent_pipeline.runner import single
from agent_pipeline.runner.single import run_task

GOOD_RESPONSE = '{"value": "ok"}'
BAD_RESPONSE = "not json"


class FakeChatModel:
    """Plays back a script; an Exception in the script is raised instead of returned."""

    def __init__(self, script: list[LLMResponse | Exception]) -> None:
        self._script: Iterator[LLMResponse | Exception] = iter(script)
        self.call_count = 0

    def complete(self, prompt: str) -> LLMResponse:
        self.call_count += 1
        step = next(self._script)
        if isinstance(step, Exception):
            raise step
        return step


class RaisingChatModel:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.call_count = 0

    def complete(self, prompt: str) -> LLMResponse:
        self.call_count += 1
        raise self._exc


def _openai_status_error(cls: type[openai.APIStatusError], status_code: int) -> Exception:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return cls("boom", response=httpx.Response(status_code, request=request), body=None)


def _attempts_for(raw_input: dict[str, str]) -> list[TaskAttempt]:
    with db.get_session() as session:
        task = session.scalars(
            select(Task).where(Task.task_key == derive_task_key(raw_input))
        ).one()
        return list(session.scalars(select(TaskAttempt).where(TaskAttempt.task_id == task.id)))


def _task_for(raw_input: dict[str, str]) -> Task:
    with db.get_session() as session:
        return session.scalars(
            select(Task).where(Task.task_key == derive_task_key(raw_input))
        ).one()


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with PostgresContainer("postgres:17", driver="psycopg") as container:
        yield container.get_connection_url()


@pytest.fixture
def prepared_db(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    get_settings.cache_clear()
    db.get_engine.cache_clear()

    engine = db.get_engine()
    Base.metadata.create_all(engine)
    try:
        yield
    finally:
        Base.metadata.drop_all(engine)
        get_settings.cache_clear()
        db.get_engine.cache_clear()


def test_second_run_of_same_input_does_not_call_the_graph_again(prepared_db: None) -> None:
    fake_llm = FakeChatModel([LLMResponse(text=GOOD_RESPONSE, token_usage=5)])
    raw_input = {"text": "hello"}

    first = run_task(raw_input, llm=fake_llm)
    second = run_task(raw_input, llm=fake_llm)

    assert first.id == second.id
    assert fake_llm.call_count == 1


def test_inputs_that_normalise_the_same_share_a_task_key(prepared_db: None) -> None:
    fake_llm = FakeChatModel([LLMResponse(text=GOOD_RESPONSE, token_usage=5)])

    first = run_task({"text": "hello"}, llm=fake_llm)
    second = run_task({"text": "  hello  "}, llm=fake_llm)

    assert first.task_key == second.task_key
    assert first.id == second.id
    assert fake_llm.call_count == 1


def test_successful_run_marks_task_succeeded_with_authoritative_attempt(
    prepared_db: None,
) -> None:
    fake_llm = FakeChatModel([LLMResponse(text=GOOD_RESPONSE, token_usage=7)])

    task = run_task({"text": "abc"}, llm=fake_llm)

    assert task.status == TaskStatus.SUCCEEDED
    assert task.authoritative_attempt_id is not None

    with db.get_session() as session:
        attempt = session.get(TaskAttempt, task.authoritative_attempt_id)
        assert attempt is not None
        assert attempt.outcome == AttemptOutcome.SUCCEEDED
        assert attempt.tokens == 7
        assert attempt.failure_category is None


def test_exhausted_parse_attempts_abandon_the_task_and_record_the_category(
    prepared_db: None,
) -> None:
    fake_llm = FakeChatModel(
        [LLMResponse(text=BAD_RESPONSE, token_usage=1) for _ in range(MAX_PARSE_ATTEMPTS)]
    )

    task = run_task({"text": "xyz"}, llm=fake_llm)

    assert task.status == TaskStatus.ABANDONED
    assert task.authoritative_attempt_id is None

    attempts = _attempts_for({"text": "xyz"})
    assert len(attempts) == 1
    assert attempts[0].outcome == AttemptOutcome.FAILED
    assert attempts[0].tokens == MAX_PARSE_ATTEMPTS
    assert attempts[0].failure_category == FailureCategory.MALFORMED_OUTPUT.value


def test_abandoned_task_is_not_executed_again(prepared_db: None) -> None:
    raw_input = {"text": "xyz"}
    run_task(
        raw_input,
        llm=FakeChatModel(
            [LLMResponse(text=BAD_RESPONSE, token_usage=1) for _ in range(MAX_PARSE_ATTEMPTS)]
        ),
    )
    second_llm = FakeChatModel([LLMResponse(text=GOOD_RESPONSE, token_usage=1)])

    task = run_task(raw_input, llm=second_llm)

    assert task.status == TaskStatus.ABANDONED
    assert second_llm.call_count == 0
    assert len(_attempts_for(raw_input)) == 1


def test_systemic_failure_propagates_instead_of_being_recorded_as_a_task_failure(
    prepared_db: None,
) -> None:
    raw_input = {"text": "auth"}
    auth_error = _openai_status_error(openai.AuthenticationError, 401)
    llm = RaisingChatModel(auth_error)

    with pytest.raises(openai.AuthenticationError):
        run_task(raw_input, llm=llm, retry_backoff=False)

    # Not transient, so call_model's RetryPolicy must not retry it even once.
    assert llm.call_count == 1

    # Not swallowed into a task failure: no attempt row, and the task is left
    # RUNNING (non-terminal) so it is picked up again once credentials are fixed.
    assert _task_for(raw_input).status == TaskStatus.RUNNING
    assert _attempts_for(raw_input) == []


def test_transient_failure_that_recovers_within_the_cap_succeeds(prepared_db: None) -> None:
    raw_input = {"text": "briefly-rate-limited"}
    llm = FakeChatModel(
        [
            _openai_status_error(openai.RateLimitError, 429),
            _openai_status_error(openai.RateLimitError, 429),
            LLMResponse(text=GOOD_RESPONSE, token_usage=4),
        ]
    )

    task = run_task(raw_input, llm=llm, retry_backoff=False)

    assert task.status == TaskStatus.SUCCEEDED
    assert llm.call_count == 3
    attempts = _attempts_for(raw_input)
    assert len(attempts) == 1
    assert attempts[0].outcome == AttemptOutcome.SUCCEEDED


def test_server_error_that_recovers_within_the_cap_succeeds(prepared_db: None) -> None:
    raw_input = {"text": "briefly-unavailable"}
    llm = FakeChatModel(
        [
            _openai_status_error(openai.InternalServerError, 503),
            _openai_status_error(openai.InternalServerError, 500),
            LLMResponse(text=GOOD_RESPONSE, token_usage=4),
        ]
    )

    task = run_task(raw_input, llm=llm, retry_backoff=False)

    assert task.status == TaskStatus.SUCCEEDED
    assert llm.call_count == 3
    attempts = _attempts_for(raw_input)
    assert len(attempts) == 1
    assert attempts[0].outcome == AttemptOutcome.SUCCEEDED


def test_persistent_transient_failure_stops_at_the_cap_and_leaves_the_task_running(
    prepared_db: None,
) -> None:
    raw_input = {"text": "rate-limited"}
    llm = RaisingChatModel(_openai_status_error(openai.RateLimitError, 429))

    task = run_task(raw_input, llm=llm, retry_backoff=False)

    # The graph's RetryPolicy retried up to its cap; run_task adds no retry of its
    # own and leaves the task non-terminal for a later run.
    assert llm.call_count == TRANSIENT_MAX_ATTEMPTS
    assert task.status == TaskStatus.RUNNING
    attempts = _attempts_for(raw_input)
    assert len(attempts) == 1
    assert attempts[0].outcome == AttemptOutcome.FAILED
    assert attempts[0].failure_category == FailureCategory.TRANSIENT.value


def test_unclassified_failure_propagates_and_writes_no_attempt(prepared_db: None) -> None:
    raw_input = {"text": "weird"}

    llm = RaisingChatModel(RuntimeError("never seen"))

    with pytest.raises(UnclassifiedFailureError):
        run_task(raw_input, llm=llm, retry_backoff=False)

    # Unknown exceptions are not retried: they reach run_task on the first failure.
    assert llm.call_count == 1

    assert _task_for(raw_input).status == TaskStatus.RUNNING
    assert _attempts_for(raw_input) == []


def test_non_retryable_failure_abandons_immediately(
    prepared_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No exception type maps to NON_RETRYABLE yet (see failures.py), so the
    # classifier is stubbed to exercise run_task's handling of that category.
    monkeypatch.setattr(single, "classify_failure", lambda exc: FailureCategory.NON_RETRYABLE)
    raw_input = {"text": "refused"}
    llm = RaisingChatModel(RuntimeError("refusal"))

    task = run_task(raw_input, llm=llm)

    assert task.status == TaskStatus.ABANDONED
    assert llm.call_count == 1
    # Abandoning still records the failure and its category (ADR-0003).
    attempts = _attempts_for(raw_input)
    assert len(attempts) == 1
    assert attempts[0].outcome == AttemptOutcome.FAILED
    assert attempts[0].failure_category == FailureCategory.NON_RETRYABLE.value
