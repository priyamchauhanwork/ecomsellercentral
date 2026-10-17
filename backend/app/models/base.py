"""Declarative base, shared column types and mixins for all models."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, MetaData, Numeric, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Stable constraint names, so Alembic migrations can drop and alter them reliably.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# Money is DECIMAL(12,2), never FLOAT.
Money = Numeric(12, 2)


def money(nullable: bool = False) -> Mapped[Decimal]:
    if nullable:
        return mapped_column(Money, nullable=True)
    return mapped_column(Money, nullable=False, server_default="0")


def bigint_pk() -> Mapped[int]:
    return mapped_column(BigInteger, primary_key=True, autoincrement=True)


def in_check(column: str, values: tuple[str, ...]) -> str:
    """SQL for a CHECK that limits a column to a fixed list of values."""
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
