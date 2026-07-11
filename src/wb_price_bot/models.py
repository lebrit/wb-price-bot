from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .domain import utcnow


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(128), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    wb_destination: Mapped[int | None] = mapped_column(Integer)
    region_label: Mapped[str | None] = mapped_column(String(200))
    quiet_start_minute: Mapped[int | None] = mapped_column(Integer)
    quiet_end_minute: Mapped[int | None] = mapped_column(Integer)
    daily_digest_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_digest_minute: Mapped[int] = mapped_column(Integer, default=540)
    last_digest_date: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    products: Mapped[list[Product]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    wb_account: Mapped[WBAccount | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )


class WBAccount(Base):
    __tablename__ = "wb_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    encrypted_session: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="active")
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="wb_account")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        Index("uq_products_user_nm", "user_id", "nm_id", unique=True),
        Index("ix_products_active", "is_active", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    nm_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(500))
    brand: Mapped[str | None] = mapped_column(String(200))
    canonical_url: Mapped[str] = mapped_column(String(500))
    threshold_kind: Mapped[str] = mapped_column(String(20))
    threshold_value: Mapped[int] = mapped_column(Integer)
    rules_json: Mapped[str] = mapped_column(Text, default="[]")
    option_id: Mapped[int | None] = mapped_column(BigInteger)
    size_name: Mapped[str | None] = mapped_column(String(100))
    supplier_id: Mapped[int | None] = mapped_column(BigInteger)
    supplier_name: Mapped[str | None] = mapped_column(String(200))
    folder_name: Mapped[str | None] = mapped_column(String(100))
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_available: Mapped[bool] = mapped_column(Boolean, default=False)
    alert_latched: Mapped[bool] = mapped_column(Boolean, default=False)
    current_price: Mapped[int | None] = mapped_column(Integer)
    reference_price: Mapped[int | None] = mapped_column(Integer)
    lowest_price: Mapped[int | None] = mapped_column(Integer)
    basic_price: Mapped[int | None] = mapped_column(Integer)
    price_source: Mapped[str] = mapped_column(String(32), default="public_api")
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="products")
    history: Mapped[list[PriceHistory]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class PriceHistory(Base):
    __tablename__ = "price_history"
    __table_args__ = (Index("ix_history_product_observed", "product_id", "observed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    price: Mapped[int | None] = mapped_column(Integer)
    basic_price: Mapped[int | None] = mapped_column(Integer)
    is_available: Mapped[bool] = mapped_column(Boolean)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    product: Mapped[Product] = relationship(back_populates="history")


class SystemState(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (Index("ix_outbox_pending", "sent_at", "available_at", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(String(200), unique=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    product_id: Mapped[int] = mapped_column(Integer)
    nm_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(500))
    canonical_url: Mapped[str] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    rule_kind: Mapped[str | None] = mapped_column(String(20))
    rule_value: Mapped[int | None] = mapped_column(Integer)
    reference_price: Mapped[int | None] = mapped_column(Integer)
    current_price: Mapped[int | None] = mapped_column(Integer)
    drop_amount: Mapped[int] = mapped_column(Integer, default=0)
    drop_basis_points: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
