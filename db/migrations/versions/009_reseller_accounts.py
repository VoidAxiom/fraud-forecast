"""Persist reseller fraud account coordination state."""

from alembic import op


revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE sim.fraud_reseller_accounts (
    account_id UUID PRIMARY KEY,
    reseller_address JSONB NOT NULL,
    delivery_address_uuid UUID NOT NULL,
    device_uuid UUID NOT NULL,
    preferred_store_ids UUID[] NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    epoch INT NOT NULL DEFAULT 1
);
"""
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON sim.fraud_reseller_accounts TO simulator_user;")
    op.execute("GRANT SELECT ON sim.fraud_reseller_accounts TO training_user;")


def downgrade() -> None:
    pass
