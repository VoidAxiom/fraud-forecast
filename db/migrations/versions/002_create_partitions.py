"""Create weekly partitions for partitioned order tables.

The partition window is anchored to the migration date (``date.today()``),
not a fixed calendar date. Each environment gets ±26 weeks of weekly
partitions centered on its install/reset date. This is intentional:

- Forward coverage: ``ensure_future_partitions(weeks_ahead)`` (defined
  below) is scheduled as a cron by Phase 7 to roll the window forward
  indefinitely. A fixed-anchor design would create stale partitions a
  year after install.
- Backward coverage: 26 weeks of past partitions accept historical
  backfill data without manual partition creation.

Consequence acknowledged: two databases migrated on different calendar
weeks have different physical partition names (e.g. ``orders_p_2026_21``
vs ``orders_p_2026_22``). Tests (Phase 1-E) assert structural properties
(count, ranges, routing correctness) rather than specific partition
names, so this divergence is benign.
"""

from datetime import date, timedelta

from alembic import op


# revision identifiers, used by Alembic.
revision = '002'
down_revision = '001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    today = date.today()
    iso_dow = today.isocalendar()[2]
    week_start = today - timedelta(days=iso_dow - 1)

    partitioned_parents = [
        ("orders", "placed_at"),
        ("orders_archive", "placed_at"),
        ("order_items", "order_placed_at"),
        ("order_items_archive", "order_placed_at"),
        ("order_events", "order_placed_at"),
        ("order_events_archive", "order_placed_at"),
    ]

    for offset in range(-26, 27):
        wk_start = week_start + timedelta(weeks=offset)
        wk_end = wk_start + timedelta(weeks=1)
        iso_y, iso_w, _ = wk_start.isocalendar()

        for parent, partition_key in partitioned_parents:
            partition_name = f"{parent}_p_{iso_y}_{iso_w:02d}"
            op.execute(
                "CREATE TABLE IF NOT EXISTS "
                f"{partition_name} PARTITION OF {parent} FOR VALUES FROM "
                f"('{wk_start.isoformat()}') TO ('{wk_end.isoformat()}')"
            )

    op.execute(
        """
CREATE OR REPLACE FUNCTION ensure_future_partitions(weeks_ahead INTEGER DEFAULT 8)
RETURNS void AS $$
DECLARE
    parent_record RECORD;
    week_offset INTEGER;
    wk_start DATE;
    wk_end DATE;
    partition_name TEXT;
BEGIN
    FOR week_offset IN 1..weeks_ahead LOOP
        wk_start := date_trunc('week', CURRENT_DATE)::date + (week_offset * INTERVAL '1 week');
        wk_end := wk_start + INTERVAL '1 week';
        FOR parent_record IN
            SELECT unnest(ARRAY[
                'orders','orders_archive',
                'order_items','order_items_archive',
                'order_events','order_events_archive'
            ]) AS parent
        LOOP
            partition_name := parent_record.parent || '_p_' ||
                to_char(wk_start, 'IYYY_IW');
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS %I PARTITION OF %I '
                'FOR VALUES FROM (%L) TO (%L)',
                partition_name, parent_record.parent,
                wk_start::text, wk_end::text
            );
        END LOOP;
    END LOOP;
END;
$$ LANGUAGE plpgsql;
"""
    )


def downgrade() -> None:
    pass
