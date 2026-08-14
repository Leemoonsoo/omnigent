"""Add the reconnecting host status.

Revision ID: zb2c3d4e5f6a
Revises: za2b3c4d5e6f
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "zb2c3d4e5f6a"
down_revision: str | None = "za2b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allow status code 3 (reconnecting) in hosts."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_constraint("ck_hosts_status", type_="check")
        batch_op.create_check_constraint("ck_hosts_status", "status IN (1, 2, 3)")


def downgrade() -> None:
    """Collapse reconnecting hosts to offline and restore the old constraint."""
    op.execute("UPDATE hosts SET status = 2 WHERE status = 3")
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_constraint("ck_hosts_status", type_="check")
        batch_op.create_check_constraint("ck_hosts_status", "status IN (1, 2)")
