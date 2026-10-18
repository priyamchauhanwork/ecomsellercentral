"""Merge ecs_channel_connection into ecs_channel_accounts; orders belong to sellers.

- ecs_channel_accounts takes over the connection's sync, webhook and credential columns;
  every connection row is copied into it, and the orders and import runs that pointed
  at a connection now point at the matching account (channel_account_id).
- store_id becomes seller_id, a foreign key to ecs_sellers. A seller row is created for
  every store id in use (store 1 → "Default seller").
- ecs_order_line.product_id / variant_id become foreign keys to ecs_articles / ecs_items.

Hand-written: Alembic can't move data. Revision ID: 0f7f1af4ca24
Revises: 3a489a0fb404
Create Date: 2026-10-18 23:30:41.911564
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0f7f1af4ca24"
down_revision: str | Sequence[str] | None = "3a489a0fb404"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACCOUNT_COLUMNS = [
    sa.Column("marketplace_region", sa.String(length=30), nullable=True),
    sa.Column("credential_ref", sa.String(length=255), nullable=True),
    sa.Column("connection_status", sa.String(length=20), server_default="active", nullable=False),
    sa.Column("sync_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    sa.Column("sync_interval_minutes", sa.Integer(), server_default="15", nullable=False),
    sa.Column("sync_cursor", sa.String(length=500), nullable=True),
    sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_sync_status", sa.String(length=20), nullable=True),
    sa.Column("webhook_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    sa.Column("webhook_secret_ref", sa.String(length=255), nullable=True),
    sa.Column("webhook_topics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column("created_by", sa.BigInteger(), nullable=True),
]
# Columns that move between ecs_channel_connection and ecs_channel_accounts unchanged.
MOVED = (
    "marketplace_region, credential_ref, connection_status, sync_enabled, "
    "sync_interval_minutes, sync_cursor, last_synced_at, last_sync_status, "
    "webhook_enabled, webhook_secret_ref, webhook_topics, created_by, created_at, updated_at"
)


def upgrade() -> None:
    # 1. Sellers for every store id in use (store 1 is the Phase 0 default seller), and
    #    channels for every marketplace a connection uses.
    op.execute("""
        INSERT INTO ecs_sellers (seller_id, name)
        SELECT store_id, CASE WHEN store_id = 1 THEN 'Default seller' ELSE 'Seller ' || store_id END
        FROM (SELECT store_id FROM ecs_order_header
              UNION SELECT store_id FROM ecs_order_import
              UNION SELECT store_id FROM ecs_channel_connection
              UNION SELECT 1) AS stores
        ON CONFLICT (seller_id) DO NOTHING
    """)
    op.execute("""
        SELECT setval(pg_get_serial_sequence('ecs_sellers', 'seller_id'),
                      (SELECT MAX(seller_id) FROM ecs_sellers))
    """)
    op.execute("""
        INSERT INTO ecs_channels (code, name)
        SELECT DISTINCT upper(channel), initcap(channel) FROM ecs_channel_connection
        ON CONFLICT (code) DO NOTHING
    """)

    # 2. ecs_channel_accounts takes the connection's columns.
    for column in ACCOUNT_COLUMNS:
        op.add_column("ecs_channel_accounts", column.copy())
    op.create_check_constraint(
        op.f("ck_ecs_channel_accounts_connection_status"),
        "ecs_channel_accounts",
        "connection_status IN ('active', 'auth_expired', 'disconnected', 'error')",
    )
    op.create_check_constraint(
        op.f("ck_ecs_channel_accounts_sync_interval_positive"),
        "ecs_channel_accounts",
        "sync_interval_minutes > 0",
    )
    op.create_unique_constraint(
        op.f("uq_ecs_channel_accounts_seller_id_channel_id_external_seller_id"),
        "ecs_channel_accounts",
        ["seller_id", "channel_id", "external_seller_id"],
    )

    # 3. Every connection becomes an account, remembering where it came from: an account
    #    for the same seller, channel and marketplace id is reused (e.g. after a
    #    downgrade and upgrade), otherwise a new one is created.
    op.add_column("ecs_channel_accounts", sa.Column("legacy_connection_id", sa.BigInteger()))
    moved = [col.strip() for col in MOVED.split(",")]
    op.execute(f"""
        UPDATE ecs_channel_accounts a
        SET legacy_connection_id = c.channel_connection_id,
            {", ".join(f"{col} = c.{col}" for col in moved if col != "created_at")}
        FROM ecs_channel_connection c
        JOIN ecs_channels ch ON ch.code = upper(c.channel)
        WHERE a.seller_id = c.store_id AND a.channel_id = ch.channel_id
          AND a.external_seller_id = left(c.external_account_id, 100)
    """)
    op.execute(f"""
        INSERT INTO ecs_channel_accounts
            (seller_id, channel_id, account_name, external_seller_id, is_active,
             legacy_connection_id, {MOVED})
        SELECT c.store_id, ch.channel_id, c.connection_name, left(c.external_account_id, 100),
               true, c.channel_connection_id, {", ".join("c." + col for col in moved)}
        FROM ecs_channel_connection c
        JOIN ecs_channels ch ON ch.code = upper(c.channel)
        WHERE NOT EXISTS (SELECT 1 FROM ecs_channel_accounts a
                          WHERE a.legacy_connection_id = c.channel_connection_id)
    """)

    # 4. Orders and import runs: store_id → seller_id, connection → account.
    for table in ("ecs_order_header", "ecs_order_import"):
        op.add_column(table, sa.Column("seller_id", sa.BigInteger(), nullable=True))
        op.add_column(table, sa.Column("channel_account_id", sa.BigInteger(), nullable=True))
        op.execute(f"""
            UPDATE {table} t SET seller_id = t.store_id,
                channel_account_id = (SELECT a.channel_account_id FROM ecs_channel_accounts a
                                      WHERE a.legacy_connection_id = t.channel_connection_id)
        """)
        op.alter_column(table, "seller_id", nullable=False)

    op.drop_constraint(
        op.f("fk_ecs_order_header_channel_connection_id_ecs_channel_c_b8bb"),
        "ecs_order_header",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("uq_ecs_order_header_store_id_channel_external_order_id"),
        "ecs_order_header",
        type_="unique",
    )
    op.drop_index(op.f("ix_ecs_order_header_store_id_order_status_order_date"), "ecs_order_header")
    op.drop_constraint(
        op.f("fk_ecs_order_import_channel_connection_id_ecs_channel_c_3faa"),
        "ecs_order_import",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("uq_ecs_order_import_channel_connection_id_external_event_id"),
        "ecs_order_import",
        type_="unique",
    )
    op.drop_index(op.f("ix_ecs_order_import_channel_connection_id_status"), "ecs_order_import")
    op.drop_index(op.f("ix_ecs_order_import_store_id_created_at"), "ecs_order_import")
    for table in ("ecs_order_header", "ecs_order_import"):
        op.drop_column(table, "store_id")
        op.drop_column(table, "channel_connection_id")

    op.create_unique_constraint(
        op.f("uq_ecs_order_header_seller_id_channel_external_order_id"),
        "ecs_order_header",
        ["seller_id", "channel", "external_order_id"],
    )
    op.create_index(
        op.f("ix_ecs_order_header_seller_id_order_status_order_date"),
        "ecs_order_header",
        ["seller_id", "order_status", "order_date"],
    )
    op.create_unique_constraint(
        op.f("uq_ecs_order_import_channel_account_id_external_event_id"),
        "ecs_order_import",
        ["channel_account_id", "external_event_id"],
    )
    op.create_index(
        op.f("ix_ecs_order_import_channel_account_id_status"),
        "ecs_order_import",
        ["channel_account_id", "status"],
    )
    op.create_index(
        op.f("ix_ecs_order_import_seller_id_created_at"),
        "ecs_order_import",
        ["seller_id", "created_at"],
    )
    for table in ("ecs_order_header", "ecs_order_import"):
        op.create_foreign_key(
            op.f(f"fk_{table}_seller_id_ecs_sellers"),
            table, "ecs_sellers", ["seller_id"], ["seller_id"],
        )
        op.create_foreign_key(
            op.f(f"fk_{table}_channel_account_id_ecs_channel_accounts"),
            table, "ecs_channel_accounts", ["channel_account_id"], ["channel_account_id"],
        )

    # 5. Order lines link to the catalog. Clear any link that doesn't exist (none should).
    op.execute("""
        UPDATE ecs_order_line SET product_id = NULL
        WHERE product_id IS NOT NULL
          AND product_id NOT IN (SELECT article_id FROM ecs_articles)
    """)
    op.execute("""
        UPDATE ecs_order_line SET variant_id = NULL
        WHERE variant_id IS NOT NULL
          AND variant_id NOT IN (SELECT item_id FROM ecs_items)
    """)
    op.create_foreign_key(
        op.f("fk_ecs_order_line_product_id_ecs_articles"),
        "ecs_order_line", "ecs_articles", ["product_id"], ["article_id"],
    )
    op.create_foreign_key(
        op.f("fk_ecs_order_line_variant_id_ecs_items"),
        "ecs_order_line", "ecs_items", ["variant_id"], ["item_id"],
    )
    op.create_index(op.f("ix_ecs_order_line_variant_id"), "ecs_order_line", ["variant_id"])

    # 6. The connection table is no longer needed.
    op.drop_column("ecs_channel_accounts", "legacy_connection_id")
    op.drop_table("ecs_channel_connection")


def downgrade() -> None:
    """Recreate ecs_channel_connection from the accounts that have a marketplace channel,
    and point orders back at it. Seller and channel rows are kept."""
    op.create_table(
        "ecs_channel_connection",
        sa.Column("channel_connection_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("store_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.String(length=30), nullable=False),
        sa.Column("connection_name", sa.String(length=100), nullable=False),
        sa.Column("external_account_id", sa.String(length=150), nullable=False),
        *(column.copy() for column in ACCOUNT_COLUMNS),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "channel IN ('shopify', 'amazon', 'flipkart', 'woocommerce')",
            name=op.f("ck_ecs_channel_connection_channel"),
        ),
        sa.CheckConstraint(
            "connection_status IN ('active', 'auth_expired', 'disconnected', 'error')",
            name=op.f("ck_ecs_channel_connection_connection_status"),
        ),
        sa.CheckConstraint(
            "sync_interval_minutes > 0", name=op.f("ck_ecs_channel_connection_sync_interval_positive")
        ),
        sa.PrimaryKeyConstraint("channel_connection_id", name=op.f("pk_ecs_channel_connection")),
        sa.UniqueConstraint(
            "store_id", "channel", "external_account_id",
            name=op.f("uq_ecs_channel_connection_store_id_channel_external_account_id"),
        ),
    )
    op.execute(f"""
        INSERT INTO ecs_channel_connection
            (channel_connection_id, store_id, channel, connection_name, external_account_id, {MOVED})
        SELECT a.channel_account_id, a.seller_id, lower(ch.code), a.account_name,
               coalesce(a.external_seller_id, a.account_name),
               {", ".join("a." + col.strip() for col in MOVED.split(","))}
        FROM ecs_channel_accounts a JOIN ecs_channels ch USING (channel_id)
        WHERE lower(ch.code) IN ('shopify', 'amazon', 'flipkart', 'woocommerce')
    """)
    op.execute("""
        SELECT setval(pg_get_serial_sequence('ecs_channel_connection', 'channel_connection_id'),
                      GREATEST((SELECT MAX(channel_connection_id) FROM ecs_channel_connection), 1))
    """)

    op.drop_index(op.f("ix_ecs_order_line_variant_id"), "ecs_order_line")
    op.drop_constraint(op.f("fk_ecs_order_line_variant_id_ecs_items"), "ecs_order_line", type_="foreignkey")
    op.drop_constraint(op.f("fk_ecs_order_line_product_id_ecs_articles"), "ecs_order_line", type_="foreignkey")

    for table in ("ecs_order_header", "ecs_order_import"):
        op.add_column(table, sa.Column("store_id", sa.BigInteger(), nullable=True))
        op.add_column(table, sa.Column("channel_connection_id", sa.BigInteger(), nullable=True))
        op.execute(f"""
            UPDATE {table} SET store_id = seller_id, channel_connection_id = channel_account_id
        """)
        op.alter_column(table, "store_id", nullable=False)
        op.drop_constraint(op.f(f"fk_{table}_channel_account_id_ecs_channel_accounts"), table, type_="foreignkey")
        op.drop_constraint(op.f(f"fk_{table}_seller_id_ecs_sellers"), table, type_="foreignkey")

    op.drop_index(op.f("ix_ecs_order_import_seller_id_created_at"), "ecs_order_import")
    op.drop_index(op.f("ix_ecs_order_import_channel_account_id_status"), "ecs_order_import")
    op.drop_constraint(
        op.f("uq_ecs_order_import_channel_account_id_external_event_id"), "ecs_order_import", type_="unique"
    )
    op.drop_index(op.f("ix_ecs_order_header_seller_id_order_status_order_date"), "ecs_order_header")
    op.drop_constraint(
        op.f("uq_ecs_order_header_seller_id_channel_external_order_id"), "ecs_order_header", type_="unique"
    )
    for table in ("ecs_order_header", "ecs_order_import"):
        op.drop_column(table, "seller_id")
        op.drop_column(table, "channel_account_id")

    op.create_index(op.f("ix_ecs_order_import_store_id_created_at"), "ecs_order_import", ["store_id", "created_at"])
    op.create_index(
        op.f("ix_ecs_order_import_channel_connection_id_status"),
        "ecs_order_import", ["channel_connection_id", "status"],
    )
    op.create_unique_constraint(
        op.f("uq_ecs_order_import_channel_connection_id_external_event_id"),
        "ecs_order_import", ["channel_connection_id", "external_event_id"],
    )
    op.create_foreign_key(
        op.f("fk_ecs_order_import_channel_connection_id_ecs_channel_c_3faa"),
        "ecs_order_import", "ecs_channel_connection", ["channel_connection_id"], ["channel_connection_id"],
    )
    op.create_index(
        op.f("ix_ecs_order_header_store_id_order_status_order_date"),
        "ecs_order_header", ["store_id", "order_status", "order_date"],
    )
    op.create_unique_constraint(
        op.f("uq_ecs_order_header_store_id_channel_external_order_id"),
        "ecs_order_header", ["store_id", "channel", "external_order_id"],
    )
    op.create_foreign_key(
        op.f("fk_ecs_order_header_channel_connection_id_ecs_channel_c_b8bb"),
        "ecs_order_header", "ecs_channel_connection", ["channel_connection_id"], ["channel_connection_id"],
    )

    op.drop_constraint(
        op.f("uq_ecs_channel_accounts_seller_id_channel_id_external_seller_id"),
        "ecs_channel_accounts", type_="unique",
    )
    op.drop_constraint(op.f("ck_ecs_channel_accounts_sync_interval_positive"), "ecs_channel_accounts", type_="check")
    op.drop_constraint(op.f("ck_ecs_channel_accounts_connection_status"), "ecs_channel_accounts", type_="check")
    for column in reversed(ACCOUNT_COLUMNS):
        op.drop_column("ecs_channel_accounts", column.name)
