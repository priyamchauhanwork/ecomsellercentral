"""inventory supply and demand

Revision ID: 5d336fc2aae0
Revises: a7c79995f837
Create Date: 2026-10-19 14:09:09.513495

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.models.base import in_check
from app.models.inventory import DEMAND_TYPES, SIDES, STOCK_REASONS, SUPPLY_TYPES
from app.models.orders import ATTENTION_REASONS

OLD_REASONS = (
    "restock", "damage", "correction", "return", "other",
    "order_created", "order_cancelled", "order_returned", "order_edited",
)
OLD_HISTORY_ACTIONS = (
    "created", "status_change", "address_update", "payment_update", "cancelled", "synced_update",
)
NEW_HISTORY_ACTIONS = (*OLD_HISTORY_ACTIONS, "acknowledged", "rto", "attention_cleared")

# Per item: every supply and demand figure, and the derived ones (docs/inventory-erd.md).
ITEM_STOCK_VIEW = """
CREATE VIEW ecs_item_stock AS
SELECT
    i.item_id,
    i.seller_id,
    COALESCE(s.onhand, 0) AS onhand,
    COALESCE(s.return_pending, 0) AS return_pending,
    COALESCE(s.damaged, 0) AS damaged,
    COALESCE(d.open, 0) AS open,
    COALESCE(d.allocated, 0) AS allocated,
    COALESCE(d.shipped, 0) AS shipped,
    COALESCE(s.onhand, 0) - COALESCE(d.open, 0) - COALESCE(d.allocated, 0)
        - COALESCE(d.shipped, 0) AS available,
    COALESCE(s.onhand, 0) - COALESCE(d.open, 0) - COALESCE(d.allocated, 0)
        - COALESCE(d.shipped, 0) + COALESCE(s.return_pending, 0) AS future_available,
    COALESCE(s.onhand, 0) - COALESCE(d.shipped, 0) AS on_shelf,
    s.next_eta
FROM ecs_items i
LEFT JOIN (
    SELECT item_id,
        SUM(quantity) FILTER (WHERE supply_type = 'ONHAND') AS onhand,
        SUM(quantity) FILTER (WHERE supply_type = 'RETURN_PENDING') AS return_pending,
        SUM(quantity) FILTER (WHERE supply_type = 'DAMAGED') AS damaged,
        MIN(eta) FILTER (WHERE supply_type = 'RETURN_PENDING') AS next_eta
    FROM ecs_inventory_supply GROUP BY item_id
) s ON s.item_id = i.item_id
LEFT JOIN (
    SELECT item_id,
        SUM(quantity) FILTER (WHERE demand_type = 'OPEN') AS open,
        SUM(quantity) FILTER (WHERE demand_type = 'ALLOCATED') AS allocated,
        SUM(quantity) FILTER (WHERE demand_type = 'SHIPPED') AS shipped
    FROM ecs_inventory_demand GROUP BY item_id
) d ON d.item_id = i.item_id
"""

# Lines whose orders count for stock: linked, and placed after the item's starting stock.
COUNTED_LINES = """
    FROM ecs_order_line l
    JOIN ecs_order_header h ON h.order_header_id = l.order_header_id AND NOT h.is_deleted
    JOIN ecs_items i ON i.item_id = l.variant_id
    WHERE i.stock_baseline_at IS NULL OR h.order_date > i.stock_baseline_at
"""


def cutover() -> None:
    """Rebuild today's stock_qty as supply and demand, so available = stock_qty."""
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT 1 FROM ecs_inventory_log WHERE reason = 'order_returned' LIMIT 1")
    ).first():
        raise RuntimeError(
            "Refund-based restocks (order_returned) were logged; the returns flow would count "
            "those units again. Resolve them before this migration (docs/inventory-erd.md)."
        )
    op.execute(
        "UPDATE ecs_order_line SET delivered_qty = shipped_qty WHERE delivered_at IS NOT NULL"
    )
    op.execute(f"""
        INSERT INTO ecs_inventory_demand
            (seller_id, item_id, order_header_id, order_line_id, demand_type, quantity)
        SELECT i.seller_id, i.item_id, l.order_header_id, l.order_line_id,
               CASE WHEN l.line_status IN ('allocated', 'packed') THEN 'ALLOCATED' ELSE 'OPEN' END,
               l.ordered_qty - l.cancelled_qty - l.shipped_qty
        {COUNTED_LINES} AND l.ordered_qty - l.cancelled_qty - l.shipped_qty > 0
        UNION ALL
        SELECT i.seller_id, i.item_id, l.order_header_id, l.order_line_id, 'SHIPPED',
               l.shipped_qty - l.delivered_qty
        {COUNTED_LINES} AND l.shipped_qty - l.delivered_qty > 0
    """)
    op.execute("""
        INSERT INTO ecs_inventory_supply (seller_id, item_id, supply_type, quantity)
        SELECT i.seller_id, i.item_id, 'ONHAND',
               i.stock_qty + COALESCE((SELECT SUM(d.quantity) FROM ecs_inventory_demand d
                                       WHERE d.item_id = i.item_id), 0)
        FROM ecs_items i
        UNION ALL
        SELECT i.seller_id, i.item_id, 'DAMAGED', 0 FROM ecs_items i
    """)
    # Delivered units of counted lines already left stock_qty (#21 deducted them at order
    # time). Logged per line as order_delivered, so the delivery rule doesn't deduct them
    # again; the ONHAND migration row is the opening figure before those deliveries.
    op.execute(f"""
        CREATE TEMP TABLE cutover_delivered ON COMMIT DROP AS
        SELECT i.item_id, l.order_line_id, l.delivered_qty AS quantity
        {COUNTED_LINES} AND l.delivered_qty > 0
    """)
    op.execute("""
        INSERT INTO ecs_inventory_log
            (item_id, side, inv_type, qty_change, stock_after, reason, note, order_line_id,
             transaction_id)
        SELECT item_id, 'DEMAND', demand_type, quantity, quantity, 'migration',
               'Supply & demand cutover', order_line_id, gen_random_uuid()
        FROM ecs_inventory_demand
        UNION ALL
        SELECT s.item_id, 'SUPPLY', s.supply_type, s.quantity + COALESCE(d.delivered, 0),
               s.quantity + COALESCE(d.delivered, 0), 'migration', 'Supply & demand cutover',
               NULL, gen_random_uuid()
        FROM ecs_inventory_supply s
        LEFT JOIN (SELECT item_id, SUM(quantity) AS delivered FROM cutover_delivered
                   GROUP BY item_id) d ON d.item_id = s.item_id AND s.supply_type = 'ONHAND'
        WHERE s.quantity + COALESCE(d.delivered, 0) <> 0
        UNION ALL
        SELECT c.item_id, 'SUPPLY', 'ONHAND', -c.quantity, s.quantity, 'order_delivered',
               'Delivered before the supply & demand cutover', c.order_line_id,
               gen_random_uuid()
        FROM cutover_delivered c
        JOIN ecs_inventory_supply s ON s.item_id = c.item_id AND s.supply_type = 'ONHAND'
    """)


# revision identifiers, used by Alembic.
revision: str = '5d336fc2aae0'
down_revision: Union[str, Sequence[str], None] = 'a7c79995f837'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Targets of the new tables' composite foreign keys, so they come first.
    op.create_unique_constraint(op.f('uq_ecs_items_item_id_seller_id'), 'ecs_items', ['item_id', 'seller_id'])
    op.create_unique_constraint(op.f('uq_ecs_order_line_order_line_id_order_header_id'), 'ecs_order_line', ['order_line_id', 'order_header_id'])
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('ecs_returns',
    sa.Column('return_id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('seller_id', sa.BigInteger(), nullable=False),
    sa.Column('order_header_id', sa.BigInteger(), nullable=False),
    sa.Column('channel', sa.String(length=30), nullable=False),
    sa.Column('channel_account_id', sa.BigInteger(), nullable=True),
    sa.Column('external_return_id', sa.String(length=100), nullable=True),
    sa.Column('return_number', sa.String(length=50), nullable=False),
    sa.Column('return_type', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('external_status', sa.String(length=20), nullable=True),
    sa.Column('expected_at', sa.Date(), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('qc_completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('qc_by', sa.BigInteger(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_by_import_id', sa.BigInteger(), nullable=True),
    sa.Column('last_import_id', sa.BigInteger(), nullable=True),
    sa.Column('external_created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('external_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version_no', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("channel IN ('csv', 'website', 'shopify', 'amazon', 'flipkart', 'woocommerce')", name=op.f('ck_ecs_returns_channel')),
    sa.CheckConstraint("return_type IN ('customer_return', 'rto')", name=op.f('ck_ecs_returns_return_type')),
    sa.CheckConstraint("status IN ('requested', 'open', 'received', 'qc_done', 'closed', 'declined', 'cancelled')", name=op.f('ck_ecs_returns_status')),
    sa.CheckConstraint('version_no >= 1', name=op.f('ck_ecs_returns_version_no_positive')),
    sa.ForeignKeyConstraint(['channel_account_id'], ['ecs_channel_accounts.channel_account_id'], name=op.f('fk_ecs_returns_channel_account_id_ecs_channel_accounts')),
    sa.ForeignKeyConstraint(['created_by_import_id'], ['ecs_order_import.order_import_id'], name=op.f('fk_ecs_returns_created_by_import_id_ecs_order_import')),
    sa.ForeignKeyConstraint(['last_import_id'], ['ecs_order_import.order_import_id'], name=op.f('fk_ecs_returns_last_import_id_ecs_order_import')),
    sa.ForeignKeyConstraint(['order_header_id'], ['ecs_order_header.order_header_id'], name=op.f('fk_ecs_returns_order_header_id_ecs_order_header')),
    sa.ForeignKeyConstraint(['seller_id'], ['ecs_sellers.seller_id'], name=op.f('fk_ecs_returns_seller_id_ecs_sellers')),
    sa.PrimaryKeyConstraint('return_id', name=op.f('pk_ecs_returns')),
    sa.UniqueConstraint('channel_account_id', 'external_return_id', name=op.f('uq_ecs_returns_channel_account_id_external_return_id'))
    )
    op.create_index(op.f('ix_ecs_returns_order_header_id'), 'ecs_returns', ['order_header_id'], unique=False)
    op.create_index(op.f('ix_ecs_returns_seller_id_status'), 'ecs_returns', ['seller_id', 'status'], unique=False)
    op.create_table('ecs_inventory_demand',
    sa.Column('demand_id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('seller_id', sa.BigInteger(), nullable=False),
    sa.Column('item_id', sa.BigInteger(), nullable=False),
    sa.Column('order_header_id', sa.BigInteger(), nullable=False),
    sa.Column('order_line_id', sa.BigInteger(), nullable=False),
    sa.Column('demand_type', sa.String(length=20), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("demand_type IN ('OPEN', 'ALLOCATED', 'SHIPPED')", name=op.f('ck_ecs_inventory_demand_demand_type')),
    sa.CheckConstraint('quantity > 0', name=op.f('ck_ecs_inventory_demand_quantity_positive')),
    sa.ForeignKeyConstraint(['item_id', 'seller_id'], ['ecs_items.item_id', 'ecs_items.seller_id'], name='fk_ecs_inventory_demand_item_seller'),
    sa.ForeignKeyConstraint(['order_line_id', 'order_header_id'], ['ecs_order_line.order_line_id', 'ecs_order_line.order_header_id'], name='fk_ecs_inventory_demand_line_header'),
    sa.ForeignKeyConstraint(['seller_id'], ['ecs_sellers.seller_id'], name=op.f('fk_ecs_inventory_demand_seller_id_ecs_sellers')),
    sa.PrimaryKeyConstraint('demand_id', name=op.f('pk_ecs_inventory_demand')),
    sa.UniqueConstraint('order_line_id', 'demand_type', name=op.f('uq_ecs_inventory_demand_order_line_id_demand_type'))
    )
    op.create_index(op.f('ix_ecs_inventory_demand_item_id_demand_type'), 'ecs_inventory_demand', ['item_id', 'demand_type'], unique=False)
    op.create_index(op.f('ix_ecs_inventory_demand_order_header_id'), 'ecs_inventory_demand', ['order_header_id'], unique=False)
    op.create_table('ecs_return_lines',
    sa.Column('return_line_id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('return_id', sa.BigInteger(), nullable=False),
    sa.Column('order_line_id', sa.BigInteger(), nullable=False),
    sa.Column('item_id', sa.BigInteger(), nullable=True),
    sa.Column('external_return_line_id', sa.String(length=100), nullable=True),
    sa.Column('external_disposal_ref', sa.String(length=100), nullable=True),
    sa.Column('return_qty', sa.Integer(), nullable=False),
    sa.Column('received_qty', sa.Integer(), server_default='0', nullable=False),
    sa.Column('accepted_qty', sa.Integer(), server_default='0', nullable=False),
    sa.Column('rejected_qty', sa.Integer(), server_default='0', nullable=False),
    sa.Column('missing_qty', sa.Integer(), server_default='0', nullable=False),
    sa.Column('opening_accepted_qty', sa.Integer(), server_default='0', nullable=False),
    sa.Column('opening_rejected_qty', sa.Integer(), server_default='0', nullable=False),
    sa.Column('return_reason', sa.String(length=50), nullable=True),
    sa.Column('customer_note', sa.Text(), nullable=True),
    sa.Column('qc_note', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'received', 'qc_done', 'cancelled')", name=op.f('ck_ecs_return_lines_status')),
    sa.CheckConstraint('accepted_qty + rejected_qty <= received_qty AND received_qty <= return_qty AND accepted_qty + rejected_qty + missing_qty <= return_qty', name=op.f('ck_ecs_return_lines_quantities_consistent')),
    sa.CheckConstraint('opening_accepted_qty <= accepted_qty AND opening_rejected_qty <= rejected_qty', name=op.f('ck_ecs_return_lines_opening_within_outcome')),
    sa.CheckConstraint('received_qty >= 0 AND accepted_qty >= 0 AND rejected_qty >= 0 AND missing_qty >= 0 AND opening_accepted_qty >= 0 AND opening_rejected_qty >= 0', name=op.f('ck_ecs_return_lines_quantities_non_negative')),
    sa.CheckConstraint('return_qty > 0', name=op.f('ck_ecs_return_lines_return_qty_positive')),
    sa.ForeignKeyConstraint(['item_id'], ['ecs_items.item_id'], name=op.f('fk_ecs_return_lines_item_id_ecs_items')),
    sa.ForeignKeyConstraint(['order_line_id'], ['ecs_order_line.order_line_id'], name=op.f('fk_ecs_return_lines_order_line_id_ecs_order_line')),
    sa.ForeignKeyConstraint(['return_id'], ['ecs_returns.return_id'], name=op.f('fk_ecs_return_lines_return_id_ecs_returns')),
    sa.PrimaryKeyConstraint('return_line_id', name=op.f('pk_ecs_return_lines')),
    sa.UniqueConstraint('return_id', 'order_line_id', name=op.f('uq_ecs_return_lines_return_id_order_line_id'))
    )
    op.create_index(op.f('ix_ecs_return_lines_item_id'), 'ecs_return_lines', ['item_id'], unique=False)
    op.create_index(op.f('ix_ecs_return_lines_order_line_id'), 'ecs_return_lines', ['order_line_id'], unique=False)
    op.create_table('ecs_inventory_supply',
    sa.Column('supply_id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('seller_id', sa.BigInteger(), nullable=False),
    sa.Column('item_id', sa.BigInteger(), nullable=False),
    sa.Column('supply_type', sa.String(length=20), nullable=False),
    sa.Column('quantity', sa.Integer(), server_default='0', nullable=False),
    sa.Column('eta', sa.Date(), nullable=True),
    sa.Column('return_line_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(supply_type = 'RETURN_PENDING') = (return_line_id IS NOT NULL)", name=op.f('ck_ecs_inventory_supply_return_line_only_pending')),
    sa.CheckConstraint("eta IS NULL OR supply_type = 'RETURN_PENDING'", name=op.f('ck_ecs_inventory_supply_eta_only_pending')),
    sa.CheckConstraint("supply_type = 'ONHAND' OR quantity >= 0", name=op.f('ck_ecs_inventory_supply_quantity_non_negative')),
    sa.CheckConstraint("supply_type IN ('ONHAND', 'RETURN_PENDING', 'DAMAGED')", name=op.f('ck_ecs_inventory_supply_supply_type')),
    sa.ForeignKeyConstraint(['item_id', 'seller_id'], ['ecs_items.item_id', 'ecs_items.seller_id'], name='fk_ecs_inventory_supply_item_seller'),
    sa.ForeignKeyConstraint(['return_line_id'], ['ecs_return_lines.return_line_id'], name=op.f('fk_ecs_inventory_supply_return_line_id_ecs_return_lines')),
    sa.ForeignKeyConstraint(['seller_id'], ['ecs_sellers.seller_id'], name=op.f('fk_ecs_inventory_supply_seller_id_ecs_sellers')),
    sa.PrimaryKeyConstraint('supply_id', name=op.f('pk_ecs_inventory_supply')),
    sa.UniqueConstraint('item_id', 'supply_type', 'return_line_id', name=op.f('uq_ecs_inventory_supply_item_id_supply_type_return_line_id'), postgresql_nulls_not_distinct=True)
    )
    op.create_index(op.f('ix_ecs_inventory_supply_return_line_id'), 'ecs_inventory_supply', ['return_line_id'], unique=False)
    op.add_column('ecs_inventory_log', sa.Column('side', sa.String(length=10), nullable=True))
    op.add_column('ecs_inventory_log', sa.Column('inv_type', sa.String(length=20), nullable=True))
    op.add_column('ecs_inventory_log', sa.Column('return_line_id', sa.BigInteger(), nullable=True))
    op.add_column('ecs_inventory_log', sa.Column('transaction_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_ecs_inventory_log_return_line_id'), 'ecs_inventory_log', ['return_line_id'], unique=False)
    op.create_index(op.f('ix_ecs_inventory_log_transaction_id'), 'ecs_inventory_log', ['transaction_id'], unique=False)
    op.create_foreign_key(op.f('fk_ecs_inventory_log_return_line_id_ecs_return_lines'), 'ecs_inventory_log', 'ecs_return_lines', ['return_line_id'], ['return_line_id'])
    op.add_column('ecs_order_header', sa.Column('attention_reason', sa.String(length=40), nullable=True))
    op.add_column('ecs_order_header', sa.Column('attention_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('ecs_order_line', sa.Column('delivered_qty', sa.Integer(), server_default='0', nullable=False))
    op.add_column('ecs_sellers', sa.Column('return_eta_days', sa.Integer(), server_default='10', nullable=False))
    # ### end Alembic commands ###

    # CHECK constraints on existing tables (autogenerate doesn't compare them).
    op.drop_constraint(op.f("ck_ecs_inventory_log_reason"), "ecs_inventory_log", type_="check")
    op.create_check_constraint("reason", "ecs_inventory_log", in_check("reason", STOCK_REASONS))
    op.create_check_constraint("side", "ecs_inventory_log", f"side IS NULL OR {in_check('side', SIDES)}")
    op.create_check_constraint(
        "inv_type",
        "ecs_inventory_log",
        f"inv_type IS NULL OR {in_check('inv_type', (*SUPPLY_TYPES, *DEMAND_TYPES))}",
    )
    op.create_check_constraint(
        "attention_reason",
        "ecs_order_header",
        f"attention_reason IS NULL OR {in_check('attention_reason', ATTENTION_REASONS)}",
    )
    op.create_check_constraint(
        "delivered_within_shipped",
        "ecs_order_line",
        "delivered_qty >= 0 AND delivered_qty <= shipped_qty",
    )
    op.create_check_constraint("return_eta_days_positive", "ecs_sellers", "return_eta_days > 0")
    op.drop_constraint(op.f("ck_ecs_order_history_action_type"), "ecs_order_history", type_="check")
    op.create_check_constraint(
        "action_type", "ecs_order_history", in_check("action_type", NEW_HISTORY_ACTIONS)
    )
    op.execute(ITEM_STOCK_VIEW)
    cutover()


def downgrade() -> None:
    """Downgrade schema. stock_qty was kept up to date, so nothing is lost."""
    op.execute("DROP VIEW ecs_item_stock")
    op.execute("DELETE FROM ecs_inventory_log WHERE side IS NOT NULL")
    op.drop_constraint(op.f("ck_ecs_order_history_action_type"), "ecs_order_history", type_="check")
    op.execute(
        "UPDATE ecs_order_history SET action_type = 'status_change'"
        " WHERE action_type IN ('acknowledged', 'rto', 'attention_cleared')"
    )
    op.create_check_constraint(
        "action_type", "ecs_order_history", in_check("action_type", OLD_HISTORY_ACTIONS)
    )
    op.execute("UPDATE ecs_order_header SET order_status = 'pending' WHERE order_status = 'processing'")
    op.drop_constraint(op.f("ck_ecs_sellers_return_eta_days_positive"), "ecs_sellers", type_="check")
    op.drop_constraint(op.f("ck_ecs_order_line_delivered_within_shipped"), "ecs_order_line", type_="check")
    op.drop_constraint(op.f("ck_ecs_order_header_attention_reason"), "ecs_order_header", type_="check")
    op.drop_constraint(op.f("ck_ecs_inventory_log_inv_type"), "ecs_inventory_log", type_="check")
    op.drop_constraint(op.f("ck_ecs_inventory_log_side"), "ecs_inventory_log", type_="check")
    op.drop_constraint(op.f("ck_ecs_inventory_log_reason"), "ecs_inventory_log", type_="check")
    op.create_check_constraint("reason", "ecs_inventory_log", in_check("reason", OLD_REASONS))
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('ecs_sellers', 'return_eta_days')
    op.drop_column('ecs_order_line', 'delivered_qty')
    op.drop_column('ecs_order_header', 'attention_at')
    op.drop_column('ecs_order_header', 'attention_reason')
    op.drop_constraint(op.f('fk_ecs_inventory_log_return_line_id_ecs_return_lines'), 'ecs_inventory_log', type_='foreignkey')
    op.drop_index(op.f('ix_ecs_inventory_log_transaction_id'), table_name='ecs_inventory_log')
    op.drop_index(op.f('ix_ecs_inventory_log_return_line_id'), table_name='ecs_inventory_log')
    op.drop_column('ecs_inventory_log', 'transaction_id')
    op.drop_column('ecs_inventory_log', 'return_line_id')
    op.drop_column('ecs_inventory_log', 'inv_type')
    op.drop_column('ecs_inventory_log', 'side')
    op.drop_index(op.f('ix_ecs_inventory_supply_return_line_id'), table_name='ecs_inventory_supply')
    op.drop_table('ecs_inventory_supply')
    op.drop_index(op.f('ix_ecs_return_lines_order_line_id'), table_name='ecs_return_lines')
    op.drop_index(op.f('ix_ecs_return_lines_item_id'), table_name='ecs_return_lines')
    op.drop_table('ecs_return_lines')
    op.drop_index(op.f('ix_ecs_inventory_demand_order_header_id'), table_name='ecs_inventory_demand')
    op.drop_index(op.f('ix_ecs_inventory_demand_item_id_demand_type'), table_name='ecs_inventory_demand')
    op.drop_table('ecs_inventory_demand')
    op.drop_index(op.f('ix_ecs_returns_seller_id_status'), table_name='ecs_returns')
    op.drop_index(op.f('ix_ecs_returns_order_header_id'), table_name='ecs_returns')
    op.drop_table('ecs_returns')
    op.drop_constraint(op.f('uq_ecs_order_line_order_line_id_order_header_id'), 'ecs_order_line', type_='unique')
    op.drop_constraint(op.f('uq_ecs_items_item_id_seller_id'), 'ecs_items', type_='unique')
    # ### end Alembic commands ###
