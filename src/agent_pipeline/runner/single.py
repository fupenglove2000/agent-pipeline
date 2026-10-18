"""Single-process, single-call task execution: check before running, write after.

No retry shell and no concurrency handling here — a second process racing on
the same task_key is a runner-level concern for later. This is just the
idempotent core of ADR-0002: skip work already done, record every execution.
"""

import json
import time
from typing import Any

from sqlalchemy import select

from agent_pipeline.db import get_session
from agent_pipeline.graph.build import build_graph
from agent_pipeline.graph.failures import FailureCategory, classify_failure
from agent_pipeline.graph.state import ParsedResult
from agent_pipeline.llm import ChatModel, OpenAIChatModel
from agent_pipeline.models import AttemptOutcome, Task, TaskAttempt, TaskStatus
from agent_pipeline.normalize import derive_task_key

TERMINAL_STATUSES = (TaskStatus.SUCCEEDED, TaskStatus.ABANDONED)


def run_task(
    raw_input: dict[str, Any], llm: ChatModel | None = None, *, retry_backoff: bool = True
) -> Task:
    task_key = derive_task_key(raw_input)

    with get_session() as session:
        existing = session.execute(
            select(Task).where(Task.task_key == task_key)
        ).scalar_one_or_none()

        if existing is not None and existing.status in TERMINAL_STATUSES:
            # Idempotency in effect: no graph call, no new TaskAttempt.
            return existing

        if existing is None:
            task = Task(task_key=task_key, status=TaskStatus.PENDING)
            session.add(task)
            session.commit()
        else:
            # Non-terminal row from a prior run (e.g. left RUNNING by a crash).
            # task_key is unique, so this is the same row picking up again —
            # not a duplicate. Racing another process for it is out of scope.
            task = existing

        task.status = TaskStatus.RUNNING
        session.commit()

        graph = build_graph(llm or OpenAIChatModel(), retry_backoff=retry_backoff)
        started = time.monotonic()

        tokens: int
        parsed_result: ParsedResult | None
        category: FailureCategory | None
        abandon: bool
        try:
            final_state = graph.invoke(
                {
                    "raw_input": json.dumps(raw_input),
                    "last_response": None,
                    "parsed_result": None,
                    "attempt_count": 0,
                    "total_tokens": 0,
                }
            )
        except Exception as exc:
            # Raises UnclassifiedFailureError for anything outside ADR-0003's table.
            category = classify_failure(exc)
            if category is FailureCategory.SYSTEMIC:
                # Abort signal: stopping the run is the caller's job, so this
                # must not be handled as one task's failure. No attempt row is
                # written either — the failure says nothing about this task,
                # and with the DB itself down the write could not succeed. The
                # task stays RUNNING and is picked up again once the cause is fixed.
                raise
            # graph.invoke raising discards its state, so tokens spent on earlier
            # parse retries within this execution are not recorded here.
            tokens, parsed_result = 0, None
            # A TRANSIENT failure reaching here has already used call_model's
            # RetryPolicy (TRANSIENT_MAX_ATTEMPTS); this layer adds no retry of its
            # own and leaves the task RUNNING for a later run. A MALFORMED_OUTPUT
            # escaping the graph is treated the same. NON_RETRYABLE is abandoned
            # immediately, per ADR-0003.
            abandon = category is FailureCategory.NON_RETRYABLE
        else:
            tokens = final_state["total_tokens"]
            parsed_result = final_state["parsed_result"]
            # No exception to classify: the graph returning without a result means
            # it ran out of MAX_PARSE_ATTEMPTS, which is MALFORMED_OUTPUT's cap
            # (ADR-0003) — so the task is abandoned.
            category = None if parsed_result is not None else FailureCategory.MALFORMED_OUTPUT
            abandon = parsed_result is None
        latency_ms = int((time.monotonic() - started) * 1000)

        attempt = TaskAttempt(
            task_id=task.id,
            tokens=tokens,
            latency_ms=latency_ms,
            outcome=AttemptOutcome.SUCCEEDED
            if parsed_result is not None
            else AttemptOutcome.FAILED,
            failure_category=category.value if category is not None else None,
        )
        session.add(attempt)
        session.flush()  # assigns attempt.id, needed below

        if parsed_result is not None:
            task.status = TaskStatus.SUCCEEDED
            task.authoritative_attempt_id = attempt.id
            task.result = parsed_result.model_dump()
        elif abandon:
            task.status = TaskStatus.ABANDONED

        session.commit()
        return task
