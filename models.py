"""SQLAlchemy models for Morning Shift's configuration and workflow state.

Reservation data is deliberately absent: it lives encrypted in the separate
PMS database and is decrypted only in memory by :mod:`pms`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class HandledFindingRecord(Base):
    __tablename__ = "handled_findings"

    check_type: Mapped[str] = mapped_column(String, primary_key=True)
    entity_lookup: Mapped[str] = mapped_column(String(64), primary_key=True)
    finding_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    handled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CancellationRuleRecord(Base):
    __tablename__ = "cancellation_rules"

    rule_key: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String)
    fee_type: Mapped[str | None] = mapped_column(String, nullable=True)
    always_late: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    days_before_arrival: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cutoff_time: Mapped[str | None] = mapped_column(String, nullable=True)
    configured: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String, default="auto")


class PropertyRecord(Base):
    __tablename__ = "properties"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    name_configured: Mapped[bool] = mapped_column(Boolean, default=False)
    name_source: Mapped[str] = mapped_column(String, default="auto")
    currency: Mapped[str] = mapped_column(String, default="EUR")
    currency_configured: Mapped[bool] = mapped_column(Boolean, default=False)
    currency_source: Mapped[str] = mapped_column(String, default="auto")


class BalanceRuleRecord(Base):
    __tablename__ = "balance_rules"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String)
    threshold: Mapped[float] = mapped_column(Float)


class BalanceRulePropertyRecord(Base):
    __tablename__ = "balance_rule_properties"

    rule_id: Mapped[str] = mapped_column(ForeignKey("balance_rules.id"), primary_key=True)
    # unique (not just part of the composite PK) enforces "a property
    # belongs to at most one rule" at the database level too, not just in
    # ThresholdRuleStore's own bookkeeping.
    property_code: Mapped[str] = mapped_column(String, primary_key=True, unique=True)


class AppSettingRecord(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)
