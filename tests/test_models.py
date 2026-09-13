from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from agent_pipeline import db
from agent_pipeline.config import get_settings
from agent_pipeline.models import Base, Task, TaskStatus, reclaimable_tasks


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with PostgresContainer("postgres:17", driver="psycopg") as container:
        yield container.get_connection_url()


@pytest.fixture
def session(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    get_settings.cache_clear()
    db.get_engine.cache_clear()

    engine = db.get_engine()
    Base.metadata.create_all(engine)
    try:
        with db.get_session() as s:
            yield s
    finally:
        Base.metadata.drop_all(engine)
        get_settings.cache_clear()
        db.get_engine.cache_clear()


def test_duplicate_task_key_is_rejected(session: Session) -> None:
    session.add(Task(task_key="same-key", status=TaskStatus.PENDING))
    session.commit()

    session.add(Task(task_key="same-key", status=TaskStatus.PENDING))
    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("terminal_status", [TaskStatus.SUCCEEDED, TaskStatus.ABANDONED])
def test_terminal_statuses_are_not_reclaimable(
    session: Session, terminal_status: TaskStatus
) -> None:
    # ADR-0002: SUCCEEDED and ABANDONED are the only terminal states. There is
    # no database constraint enforcing this — it's an application-layer rule —
    # so this asserts it at that layer: a terminal task never shows up as
    # reclaimable, no matter what lease_expires_at says.
    expired = datetime.now(UTC) - timedelta(hours=1)
    session.add(Task(task_key="terminal", status=terminal_status, lease_expires_at=expired))
    session.commit()

    result = session.scalars(reclaimable_tasks(now=datetime.now(UTC))).all()

    assert result == []


def test_running_with_expired_lease_is_reclaimable(session: Session) -> None:
    now = datetime.now(UTC)
    expired = Task(
        task_key="expired", status=TaskStatus.RUNNING, lease_expires_at=now - timedelta(minutes=1)
    )
    not_expired = Task(
        task_key="not-expired",
        status=TaskStatus.RUNNING,
        lease_expires_at=now + timedelta(minutes=1),
    )
    pending = Task(task_key="pending", status=TaskStatus.PENDING)
    session.add_all([expired, not_expired, pending])
    session.commit()

    result = session.scalars(reclaimable_tasks(now=now)).all()

    assert [task.task_key for task in result] == ["expired"]


def test_running_without_a_lease_is_not_reclaimable(session: Session) -> None:
    # lease_expires_at is nullable; a NULL lease has nothing to compare against
    # `< now`, so SQL's NULL semantics already exclude it — this pins that down.
    session.add(Task(task_key="no-lease", status=TaskStatus.RUNNING, lease_expires_at=None))
    session.commit()

    result = session.scalars(reclaimable_tasks(now=datetime.now(UTC))).all()

    assert result == []
