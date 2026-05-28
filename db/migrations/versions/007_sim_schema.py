"""Introduce sim schema and migrate simulator_ground_truth from public."""

from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA sim")
    op.execute(
        "GRANT USAGE ON SCHEMA sim TO training_user, simulator_user, app, analyst_user"
    )
    op.execute("ALTER TABLE public.simulator_ground_truth SET SCHEMA sim")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA sim "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO simulator_user"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA sim "
        "GRANT SELECT ON TABLES TO training_user"
    )


def downgrade() -> None:
    pass
