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
from agent_pipeline.llm import ChatModel, OpenAIChatModel
from agent_pipeline.models import AttemptOutcome, Task, TaskAttempt, TaskStatus
from agent_pipeline.normalize import derive_task_key

TERMINAL_STATUSES = (TaskStatus.SUCCEEDED, TaskStatus.ABANDONED)


def run_task(raw_input: dict[str, Any], llm: ChatModel | None = None) -> Task:
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

        graph = build_graph(llm or OpenAIChatModel())
        started = time.monotonic()
        final_state = graph.invoke(
            {
                "raw_input": json.dumps(raw_input),
                "last_response": None,
                "parsed_result": None,
                "attempt_count": 0,
                "total_tokens": 0,
            }
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        parsed_result = final_state["parsed_result"]
        succeeded = parsed_result is not None

        attempt = TaskAttempt(
            task_id=task.id,
            tokens=final_state["total_tokens"],
            latency_ms=latency_ms,
            outcome=AttemptOutcome.SUCCEEDED if succeeded else AttemptOutcome.FAILED,
        )
        session.add(attempt)
        session.flush()  # assigns attempt.id, needed below

        if succeeded:
            task.status = TaskStatus.SUCCEEDED
            task.authoritative_attempt_id = attempt.id
            task.result = parsed_result.model_dump()
        else:
            # Exhausted MAX_PARSE_ATTEMPTS without a parsed result. This is not
            # ABANDONED: per ADR-0002, abandoning a task is a classification
            # decision, and that classification is ADR-0003's job (not yet
            # decided or wired in). Without it we cannot tell "still worth
            # retrying" apart from "should give up", so the task stays RUNNING
            # — reclaimable later once that policy exists.
            pass

        session.commit()
        return task
