"""Persist collusive merchant store IDs in sim schema."""

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE sim.fraud_collusive_stores (
    store_id UUID PRIMARY KEY REFERENCES public.stores(store_id),
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    epoch INT NOT NULL DEFAULT 1
);
GRANT SELECT, INSERT, UPDATE ON sim.fraud_collusive_stores TO simulator_user;
GRANT SELECT ON sim.fraud_collusive_stores TO training_user;
"""
    )


def downgrade() -> None:
    pass
