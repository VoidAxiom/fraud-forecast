"""Create refunds table."""

from alembic import op


# revision identifiers, used by Alembic.
revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE refunds (
    refund_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id         UUID NOT NULL,
    order_placed_at  TIMESTAMPTZ NOT NULL,
    amount_pence     BIGINT NOT NULL,
    reason           VARCHAR(100),
    initiated_by     VARCHAR(20),   -- USER, MERCHANT, SYSTEM
    issued_at        TIMESTAMPTZ NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""
    )
    op.execute("ALTER TABLE refunds ADD CONSTRAINT refunds_order_id_unique UNIQUE (order_id);")
    op.execute("REVOKE ALL ON refunds FROM scoring_user;")
    op.execute("REVOKE SELECT ON refunds FROM PUBLIC;")
    op.execute("REVOKE ALL ON refunds FROM simulator_user;")
    op.execute("GRANT INSERT ON refunds TO simulator_user;")
    op.execute("GRANT SELECT ON refunds TO simulator_user;")
    op.execute("GRANT SELECT ON refunds TO analyst_user;")
    op.execute("GRANT SELECT ON simulator_ground_truth TO simulator_user;")


def downgrade() -> None:
    pass
