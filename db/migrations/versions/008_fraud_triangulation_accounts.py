"""Create persistent triangulation fraudster account identities."""

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sim.fraud_triangulation_accounts (
            account_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            device_id  UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON sim.fraud_triangulation_accounts TO simulator_user"
    )
    op.execute("GRANT SELECT ON sim.fraud_triangulation_accounts TO training_user")


def downgrade() -> None:
    pass
