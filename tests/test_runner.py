from collections.abc import Iterator

import pytest
from sqlalchemy import select
from testcontainers.community.postgres import PostgresContainer

from agent_pipeline import db
from agent_pipeline.config import get_settings
from agent_pipeline.graph.build import MAX_PARSE_ATTEMPTS
from agent_pipeline.llm import LLMResponse
from agent_pipeline.models import AttemptOutcome, Base, TaskAttempt, TaskStatus
from agent_pipeline.runner.single import run_task

GOOD_RESPONSE = '{"value": "ok"}'
BAD_RESPONSE = "not json"


class FakeChatModel:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses: Iterator[LLMResponse] = iter(responses)
        self.call_count = 0

    def complete(self, prompt: str) -> LLMResponse:
        self.call_count += 1
        return next(self._responses)


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


def test_exhausted_retries_stay_running_but_record_a_failed_attempt(prepared_db: None) -> None:
    fake_llm = FakeChatModel(
        [LLMResponse(text=BAD_RESPONSE, token_usage=1) for _ in range(MAX_PARSE_ATTEMPTS)]
    )

    task = run_task({"text": "xyz"}, llm=fake_llm)

    assert task.status == TaskStatus.RUNNING
    assert task.authoritative_attempt_id is None

    with db.get_session() as session:
        attempts = session.scalars(select(TaskAttempt).where(TaskAttempt.task_id == task.id)).all()
        assert len(attempts) == 1
        assert attempts[0].outcome == AttemptOutcome.FAILED
        assert attempts[0].tokens == MAX_PARSE_ATTEMPTS
