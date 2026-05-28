"""Create simulator metadata key-value table."""

from alembic import op


revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE sim.simulator_meta (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON sim.simulator_meta TO simulator_user")
    op.execute("GRANT SELECT ON sim.simulator_meta TO training_user")
    op.execute("REVOKE DELETE ON sim.simulator_meta FROM simulator_user")


def downgrade() -> None:
    pass
