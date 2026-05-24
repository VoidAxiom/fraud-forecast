"""Create simulator ground-truth table and deny scoring-user access."""

from alembic import op


# revision identifiers, used by Alembic.
revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE simulator_ground_truth (
    order_id        UUID PRIMARY KEY,
    is_fraud        BOOLEAN NOT NULL,
    fraud_category  VARCHAR(50),
    pattern_notes   TEXT,
    ring_id         UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""
    )
    op.execute("CREATE INDEX idx_gt_fraud ON simulator_ground_truth(is_fraud);")
    op.execute("CREATE INDEX idx_gt_category ON simulator_ground_truth(fraud_category);")
    op.execute("CREATE INDEX idx_gt_ring ON simulator_ground_truth(ring_id);")
    op.execute("REVOKE ALL ON simulator_ground_truth FROM scoring_user;")
    op.execute("REVOKE SELECT ON simulator_ground_truth FROM PUBLIC;")
    # simulator_user must be INSERT-only (override the broad DML grant from 003's default privileges)
    op.execute("REVOKE ALL ON simulator_ground_truth FROM simulator_user;")
    op.execute("GRANT INSERT ON simulator_ground_truth TO simulator_user;")
    # simulator_user is INSERT-only (cannot read/update/delete labels -- append-only integrity).
    # analyst_user has SELECT for the monitoring dashboard.
    op.execute("GRANT SELECT ON simulator_ground_truth TO analyst_user;")


def downgrade() -> None:
    pass
