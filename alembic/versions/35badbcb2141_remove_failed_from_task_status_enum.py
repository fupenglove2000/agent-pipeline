"""remove FAILED from task_status enum

Revision ID: 35badbcb2141
Revises: 7037f873f0a1
Create Date: 2026-09-13 15:49:16.504185

Autogenerate produces an empty diff for this: it does not detect changes to
the value set of a Postgres native ENUM. Postgres also has no `ALTER TYPE
... DROP VALUE`, so removing a value means the standard rename/recreate/swap
dance below. If any row currently has status='FAILED', the USING cast in
upgrade() fails loudly instead of silently dropping data — that row has to be
moved to a real status by hand first.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "35badbcb2141"
down_revision: str | Sequence[str] | None = "7037f873f0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_VALUES = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "ABANDONED")
NEW_VALUES = ("PENDING", "RUNNING", "SUCCEEDED", "ABANDONED")


def upgrade() -> None:
    op.execute("ALTER TYPE task_status RENAME TO task_status_old")
    sa.Enum(*NEW_VALUES, name="task_status").create(op.get_bind())
    op.execute(
        "ALTER TABLE tasks ALTER COLUMN status TYPE task_status USING status::text::task_status"
    )
    op.execute("DROP TYPE task_status_old")


def downgrade() -> None:
    op.execute("ALTER TYPE task_status RENAME TO task_status_new")
    sa.Enum(*OLD_VALUES, name="task_status").create(op.get_bind())
    op.execute(
        "ALTER TABLE tasks ALTER COLUMN status TYPE task_status USING status::text::task_status"
    )
    op.execute("DROP TYPE task_status_new")
