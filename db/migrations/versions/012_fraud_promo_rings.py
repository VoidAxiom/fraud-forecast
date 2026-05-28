"""Persist promo-abuse ring state in sim schema."""

from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE sim.fraud_promo_rings (
    ring_id          UUID PRIMARY KEY,
    device_id        UUID NOT NULL,
    base_address     JSONB NOT NULL,
    payment_pool     JSONB NOT NULL,
    email_pattern    TEXT NOT NULL,
    created_user_ids UUID[] NOT NULL DEFAULT '{}'::UUID[],
    base_ip_prefix   TEXT NOT NULL DEFAULT '192.168.1',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON sim.fraud_promo_rings TO simulator_user"
    )
    op.execute("GRANT SELECT ON sim.fraud_promo_rings TO training_user")


def downgrade() -> None:
    pass
