"""Create training_user with grants for Phase 5 ML training.

This deliberately deviates from spec/PHASE_5.md: the spec lists only static
GRANT SELECT statements on named tables and omits ALTER DEFAULT PRIVILEGES.
The orders table is weekly-partitioned through ensure_future_partitions(), so
partition children created after this migration will not inherit the static
GRANT. The ALTER DEFAULT PRIVILEGES statement keeps SELECT grants automatic for
future partition children, matching the 003_create_roles.py pattern for
scoring_user, simulator_user, and analyst_user. This is a director-confirmed
deliberate deviation.
"""

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
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO training_user;"
    )


def downgrade() -> None:
    # Forward-only; training_user role removal not automated.
    pass
