"""SQLAlchemy models for task-level idempotency (ADR-0002).

Terminal states are SUCCEEDED and ABANDONED only. RUNNING with an expired
lease is not terminal — it must stay reclaimable, which is what
`reclaimable_tasks` finds.
"""

import enum
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, Select, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TaskStatus(enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    ABANDONED = "ABANDONED"


class AttemptOutcome(enum.Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status"), default=TaskStatus.PENDING
    )
    # Snapshot of the authoritative result, frozen once an attempt is accepted.
    result: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    authoritative_attempt_id: Mapped[int | None] = mapped_column(
        # use_alter breaks the circular FK: TaskAttempt.task_id points back here.
        ForeignKey("task_attempts.id", use_alter=True, name="fk_tasks_authoritative_attempt_id"),
        nullable=True,
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    attempts: Mapped[list["TaskAttempt"]] = relationship(
        back_populates="task", foreign_keys="TaskAttempt.task_id"
    )


class TaskAttempt(Base):
    __tablename__ = "task_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    tokens: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[AttemptOutcome] = mapped_column(Enum(AttemptOutcome, name="attempt_outcome"))
    # Populated once ADR-0003's failure classification lands. Left unset until then.
    failure_category: Mapped[str | None] = mapped_column(String, nullable=True)

    task: Mapped["Task"] = relationship(back_populates="attempts", foreign_keys=[task_id])


def reclaimable_tasks(now: datetime) -> Select[tuple[Task]]:
    """Tasks stuck in RUNNING past their lease — not terminal, safe to retry."""
    return select(Task).where(Task.status == TaskStatus.RUNNING, Task.lease_expires_at < now)
