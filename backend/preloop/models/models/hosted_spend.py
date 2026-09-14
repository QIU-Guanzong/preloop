"""Durable operator-paid model balances, independent of analytics retention."""

from datetime import date, datetime
from decimal import Decimal
import uuid

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class HostedSpendAccount(Base):
    __tablename__ = "hosted_spend_account"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # NULL means prior spending has not been proven, never a fresh grant.
    lifetime_spent: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 10), nullable=True
    )
    lifetime_reserved: Mapped[Decimal] = mapped_column(
        Numeric(20, 10), nullable=False, default=Decimal(0)
    )
    coverage_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    baseline_evidence: Mapped[str | None] = mapped_column(String(500), nullable=True)


class HostedSpendMonth(Base):
    __tablename__ = "hosted_spend_month"
    __table_args__ = (
        UniqueConstraint("account_id", "period", name="uq_hosted_spend_month"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )
    period: Mapped[date] = mapped_column(Date, nullable=False)
    spent: Mapped[Decimal] = mapped_column(
        Numeric(20, 10), nullable=False, default=Decimal(0)
    )
    reserved: Mapped[Decimal] = mapped_column(
        Numeric(20, 10), nullable=False, default=Decimal(0)
    )


class HostedSpendReservation(Base):
    __tablename__ = "hosted_spend_reservation"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "operation_key", name="uq_hosted_spend_operation"
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    operation_key: Mapped[str] = mapped_column(String(128), nullable=False)
    period: Mapped[date] = mapped_column(Date, nullable=False)
    reserved: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    actual: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
