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
    order_id          UUID NOT NULL,
    order_placed_at   TIMESTAMPTZ NOT NULL,
    fraud_pattern     VARCHAR(40) NOT NULL,
    injection_seed    BIGINT NOT NULL,
    scenario_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (order_id, order_placed_at),
    CONSTRAINT gt_pattern_check CHECK (fraud_pattern IN (
        'stolen_card', 'account_takeover', 'promo_abuse',
        'friendly_fraud', 'refund_abuse', 'collusion', 'bot_attack'
    ))
);
"""
    )
    op.execute("CREATE INDEX idx_gt_pattern ON simulator_ground_truth(fraud_pattern);")
    op.execute("CREATE INDEX idx_gt_created ON simulator_ground_truth(created_at);")
    op.execute("REVOKE ALL ON simulator_ground_truth FROM scoring_user;")
    op.execute("REVOKE ALL ON simulator_ground_truth FROM PUBLIC;")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON simulator_ground_truth TO simulator_user;"
    )
    op.execute("GRANT SELECT ON simulator_ground_truth TO app;")
    op.execute("GRANT SELECT ON simulator_ground_truth TO analyst_user;")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM scoring_user;")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS simulator_ground_truth;")
