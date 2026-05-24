"""Create database roles and scoped grants for P1-B components."""

from alembic import op


# revision identifiers, used by Alembic.
revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'scoring_user') THEN
        CREATE ROLE scoring_user WITH LOGIN PASSWORD 'scoring_dev_password';
    END IF;
END $$;
"""
    )
    op.execute("GRANT CONNECT ON DATABASE fraud_platform TO scoring_user;")
    op.execute("GRANT USAGE ON SCHEMA public TO scoring_user;")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO scoring_user;")
    op.execute("GRANT INSERT ON fraud_decisions TO scoring_user;")
    op.execute(
        "GRANT UPDATE (fraud_score, fraud_score_version, fraud_decision, fraud_rules_triggered, updated_at) "
        "ON orders TO scoring_user;"
    )
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE fraud_decisions_decision_id_seq TO scoring_user;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO scoring_user;"
    )

    op.execute(
        """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'simulator_user') THEN
        CREATE ROLE simulator_user WITH LOGIN PASSWORD 'simulator_dev_password';
    END IF;
END $$;
"""
    )
    op.execute("GRANT CONNECT ON DATABASE fraud_platform TO simulator_user;")
    op.execute("GRANT USAGE ON SCHEMA public TO simulator_user;")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO simulator_user;"
    )
    op.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO simulator_user;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO simulator_user;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO simulator_user;"
    )

    op.execute(
        """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analyst_user') THEN
        CREATE ROLE analyst_user WITH LOGIN PASSWORD 'analyst_dev_password';
    END IF;
END $$;
"""
    )
    op.execute("GRANT CONNECT ON DATABASE fraud_platform TO analyst_user;")
    op.execute("GRANT USAGE ON SCHEMA public TO analyst_user;")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO analyst_user;")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO analyst_user;"
    )


def downgrade() -> None:
    pass
