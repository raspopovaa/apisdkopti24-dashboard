"""Create storage tables

Revision ID: 0001
Revises:

Суммы, литры и координаты — текст: Decimal без потерь и в SQLite.
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "balance_snapshots",
        sa.Column("contract_key", sa.String(length=64), nullable=False),
        sa.Column("taken_at", sa.DateTime(), nullable=False),
        sa.Column("available", sa.String(length=32), nullable=False),
        sa.Column("own_funds", sa.String(length=32), nullable=True),
        sa.Column("month_spent", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("contract_key", "taken_at"),
    )
    op.create_table(
        "cards",
        sa.Column("contract_key", sa.String(length=64), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("number", sa.String(length=64), nullable=False),
        sa.Column("group_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("product", sa.String(length=255), nullable=False),
        sa.Column("home_region", sa.String(length=255), nullable=False),
        sa.Column("comment", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("contract_key", "id"),
    )
    op.create_table(
        "limit_snapshots",
        sa.Column("contract_key", sa.String(length=64), nullable=False),
        sa.Column("card_id", sa.String(length=64), nullable=False),
        sa.Column("product", sa.String(length=255), nullable=False),
        sa.Column("taken_at", sa.DateTime(), nullable=False),
        sa.Column("liters_per_month", sa.String(length=32), nullable=False),
        sa.Column("liters_used", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("contract_key", "card_id", "product", "taken_at"),
    )
    op.create_table(
        "stations",
        sa.Column("contract_key", sa.String(length=64), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("brand", sa.String(length=255), nullable=False),
        sa.Column("region", sa.String(length=255), nullable=False),
        sa.Column("latitude", sa.String(length=32), nullable=True),
        sa.Column("longitude", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("contract_key", "id"),
    )
    op.create_table(
        "sync_state",
        sa.Column("contract_key", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("loaded_until", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=False),
        sa.Column("billable_calls", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("contract_key", "source"),
    )
    op.create_table(
        "transactions",
        sa.Column("contract_key", sa.String(length=64), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("local_time", sa.DateTime(), nullable=False),
        sa.Column("utc_time", sa.DateTime(), nullable=True),
        sa.Column("card_id", sa.String(length=64), nullable=False),
        sa.Column("station_id", sa.String(length=64), nullable=False),
        sa.Column("operation_type", sa.String(length=8), nullable=True),
        sa.Column("product", sa.String(length=255), nullable=False),
        sa.Column("qty", sa.String(length=32), nullable=False),
        sa.Column("price", sa.String(length=32), nullable=False),
        sa.Column("price_no_discount", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.String(length=32), nullable=False),
        sa.Column("amount_no_discount", sa.String(length=32), nullable=True),
        sa.Column("discount", sa.String(length=32), nullable=False),
        sa.Column("is_storno", sa.Boolean(), nullable=False),
        sa.Column("storno_of", sa.String(length=64), nullable=True),
        sa.Column("raw", sa.Text(), nullable=True),
        sa.Column("loaded_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("contract_key", "id"),
    )
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_transactions_card", ["contract_key", "card_id", "local_time"], unique=False
        )
        batch_op.create_index(
            "ix_transactions_period", ["contract_key", "local_time"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.drop_index("ix_transactions_period")
        batch_op.drop_index("ix_transactions_card")

    op.drop_table("transactions")
    op.drop_table("sync_state")
    op.drop_table("stations")
    op.drop_table("limit_snapshots")
    op.drop_table("cards")
    op.drop_table("balance_snapshots")
