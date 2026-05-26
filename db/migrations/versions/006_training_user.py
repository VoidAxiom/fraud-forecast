"""Create training_user role with SELECT grants for Phase 5 ML training."""

from alembic import op


# revision identifiers, used by Alembic.
revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'training_user') THEN
        CREATE ROLE training_user WITH LOGIN PASSWORD 'training_dev_password';
    END IF;
END $$;
"""
    )
    op.execute("GRANT CONNECT ON DATABASE fraud_platform TO training_user;")
    op.execute("GRANT USAGE ON SCHEMA public TO training_user;")
    op.execute(
        "GRANT SELECT ON orders, orders_archive, order_items, order_items_archive, "
        "order_events, order_events_archive, chargebacks, refunds, "
        "simulator_ground_truth, users, devices, payment_methods, stores, "
        "merchants, user_addresses TO training_user;"
    )

def downgrade() -> None:
    # Forward-only; training_user role removal not automated.
    pass
