from __future__ import annotations

import asyncio
import sqlite3
from collections import defaultdict
from collections.abc import Sequence, Set
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from sqlalchemy import delete, event, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .domain import AlertDecision, PriceSnapshot, ThresholdKind, evaluate_alert, utcnow
from .models import (
    Base,
    NotificationOutbox,
    PriceHistory,
    Product,
    SystemState,
    User,
    WBAccount,
)


class ProductAlreadyExistsError(RuntimeError):
    pass


class ProductLimitError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WatchTarget:
    product_id: int
    user_id: int
    telegram_id: int
    nm_id: int
    encrypted_session: str | None
    account_status: str | None


@dataclass(frozen=True, slots=True)
class PendingAlert:
    outbox_id: int
    telegram_id: int
    product_id: int
    nm_id: int
    title: str
    canonical_url: str
    source: str
    decision: AlertDecision
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class DatabaseStats:
    products_total: int
    products_active: int
    users_total: int
    last_monitor_cycle: datetime | None
    alerts_pending: int


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{path.as_posix()}",
            pool_pre_ping=True,
        )

        @event.listens_for(self._engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection: object, _: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self._user_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    async def ping(self) -> None:
        async with self._sessions() as session:
            await session.scalar(select(func.count()).select_from(SystemState))

    async def ensure_user(
        self,
        telegram_id: int,
        username: str | None,
        display_name: str,
        *,
        is_admin: bool,
    ) -> User:
        async with self._sessions() as session, session.begin():
            user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user is None:
                user = User(
                    telegram_id=telegram_id,
                    username=username,
                    display_name=display_name[:128],
                    is_admin=is_admin,
                )
                session.add(user)
            else:
                user.username = username
                user.display_name = display_name[:128]
                user.is_admin = user.is_admin or is_admin
                user.is_enabled = True
            await session.flush()
            return user

    async def get_user(self, telegram_id: int) -> User | None:
        async with self._sessions() as session:
            return cast(
                User | None,
                await session.scalar(select(User).where(User.telegram_id == telegram_id)),
            )

    async def add_product(
        self,
        *,
        telegram_id: int,
        snapshot: PriceSnapshot,
        threshold_kind: ThresholdKind,
        threshold_value: int,
        max_products: int,
    ) -> Product:
        async with self._user_locks[telegram_id]:
            async with self._sessions() as session, session.begin():
                user = await self._require_user(session, telegram_id)
                count = await session.scalar(
                    select(func.count()).select_from(Product).where(Product.user_id == user.id)
                )
                if int(count or 0) >= max_products:
                    raise ProductLimitError(f"Достигнут лимит: {max_products} товаров")
                duplicate = await session.scalar(
                    select(Product).where(
                        Product.user_id == user.id, Product.nm_id == snapshot.nm_id
                    )
                )
                if duplicate is not None:
                    raise ProductAlreadyExistsError("Этот товар уже отслеживается")

                product = Product(
                    user_id=user.id,
                    nm_id=snapshot.nm_id,
                    title=snapshot.title[:500],
                    brand=snapshot.brand[:200] if snapshot.brand else None,
                    canonical_url=snapshot.url,
                    threshold_kind=threshold_kind.value,
                    threshold_value=threshold_value,
                    is_available=snapshot.available,
                    current_price=snapshot.price,
                    reference_price=snapshot.price,
                    lowest_price=snapshot.price,
                    basic_price=snapshot.basic_price,
                    price_source=snapshot.source,
                    quantity=snapshot.quantity,
                    last_checked_at=snapshot.observed_at,
                )
                session.add(product)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    raise ProductAlreadyExistsError("Этот товар уже отслеживается") from exc
                session.add(
                    PriceHistory(
                        product_id=product.id,
                        price=snapshot.price,
                        basic_price=snapshot.basic_price,
                        is_available=snapshot.available,
                        quantity=snapshot.quantity,
                        source=snapshot.source,
                        observed_at=snapshot.observed_at,
                    )
                )
                return product

    async def list_products(
        self, telegram_id: int, *, offset: int = 0, limit: int = 10
    ) -> tuple[list[Product], int]:
        async with self._sessions() as session:
            user_id = await session.scalar(select(User.id).where(User.telegram_id == telegram_id))
            if user_id is None:
                return [], 0
            total = int(
                await session.scalar(
                    select(func.count()).select_from(Product).where(Product.user_id == user_id)
                )
                or 0
            )
            products = list(
                (
                    await session.scalars(
                        select(Product)
                        .where(Product.user_id == user_id)
                        .order_by(Product.created_at.desc())
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
            )
            return products, total

    async def get_product(self, telegram_id: int, product_id: int) -> Product | None:
        async with self._sessions() as session:
            return cast(
                Product | None,
                await session.scalar(
                    select(Product)
                    .join(User, Product.user_id == User.id)
                    .where(Product.id == product_id, User.telegram_id == telegram_id)
                ),
            )

    async def get_first_product(self, telegram_id: int) -> Product | None:
        async with self._sessions() as session:
            return cast(
                Product | None,
                await session.scalar(
                    select(Product)
                    .join(User, Product.user_id == User.id)
                    .where(User.telegram_id == telegram_id)
                    .order_by(Product.created_at.asc())
                    .limit(1)
                ),
            )

    async def delete_product(self, telegram_id: int, product_id: int) -> bool:
        async with self._sessions() as session, session.begin():
            product = await self._owned_product(session, telegram_id, product_id)
            if product is None:
                return False
            await session.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.product_id == product.id,
                    NotificationOutbox.sent_at.is_(None),
                )
                .values(sent_at=utcnow(), last_error="discarded: product deleted")
            )
            await session.delete(product)
            return True

    async def toggle_product(self, telegram_id: int, product_id: int) -> bool | None:
        async with self._sessions() as session, session.begin():
            product = await self._owned_product(session, telegram_id, product_id)
            if product is None:
                return None
            product.is_active = not product.is_active
            return product.is_active

    async def recent_history(
        self, telegram_id: int, product_id: int, *, limit: int = 10
    ) -> list[PriceHistory]:
        async with self._sessions() as session:
            owned = await session.scalar(
                select(Product.id)
                .join(User, Product.user_id == User.id)
                .where(Product.id == product_id, User.telegram_id == telegram_id)
            )
            if owned is None:
                return []
            return list(
                (
                    await session.scalars(
                        select(PriceHistory)
                        .where(PriceHistory.product_id == product_id)
                        .order_by(PriceHistory.observed_at.desc())
                        .limit(limit)
                    )
                ).all()
            )

    async def save_wb_account(self, telegram_id: int, encrypted_session: str) -> None:
        async with self._sessions() as session, session.begin():
            user = await self._require_user(session, telegram_id)
            account = await session.scalar(select(WBAccount).where(WBAccount.user_id == user.id))
            if account is None:
                session.add(
                    WBAccount(
                        user_id=user.id,
                        encrypted_session=encrypted_session,
                        status="active",
                    )
                )
            else:
                account.encrypted_session = encrypted_session
                account.status = "active"
                account.last_error = None
                account.last_validated_at = None
            products = (
                await session.scalars(select(Product).where(Product.user_id == user.id))
            ).all()
            for product in products:
                product.reference_price = None
                product.alert_latched = False
                product.price_source = "context_reset"

    async def get_wb_account(self, telegram_id: int) -> WBAccount | None:
        async with self._sessions() as session:
            return cast(
                WBAccount | None,
                await session.scalar(
                    select(WBAccount)
                    .join(User, WBAccount.user_id == User.id)
                    .where(User.telegram_id == telegram_id)
                ),
            )

    async def delete_wb_account(self, telegram_id: int) -> bool:
        async with self._sessions() as session, session.begin():
            account = await session.scalar(
                select(WBAccount)
                .join(User, WBAccount.user_id == User.id)
                .where(User.telegram_id == telegram_id)
            )
            if account is None:
                return False
            await session.delete(account)
            return True

    async def set_account_result(
        self, user_id: int, *, success: bool, error: str | None = None
    ) -> None:
        async with self._sessions() as session, session.begin():
            account = await session.scalar(select(WBAccount).where(WBAccount.user_id == user_id))
            if account is None:
                return
            if success:
                account.status = "active"
                account.last_validated_at = utcnow()
                account.last_error = None
            else:
                account.status = "error"
                account.last_error = (error or "Ошибка проверки сессии")[:500]

    async def refresh_wb_account_session(self, user_id: int, encrypted_session: str) -> None:
        async with self._sessions() as session, session.begin():
            account = await session.scalar(select(WBAccount).where(WBAccount.user_id == user_id))
            if account is None:
                return
            account.encrypted_session = encrypted_session
            account.status = "active"
            account.last_validated_at = utcnow()
            account.last_error = None

    async def active_targets(self, allowed_telegram_ids: Set[int]) -> list[WatchTarget]:
        if not allowed_telegram_ids:
            return []
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(
                        Product.id,
                        Product.user_id,
                        User.telegram_id,
                        Product.nm_id,
                        WBAccount.encrypted_session,
                        WBAccount.status,
                    )
                    .join(User, Product.user_id == User.id)
                    .outerjoin(WBAccount, WBAccount.user_id == User.id)
                    .where(
                        Product.is_active.is_(True),
                        User.is_enabled.is_(True),
                        User.telegram_id.in_(allowed_telegram_ids),
                    )
                    .order_by(Product.user_id, Product.id)
                )
            ).all()
            return [WatchTarget(*row) for row in rows]

    async def apply_snapshot(self, product_id: int, snapshot: PriceSnapshot) -> PendingAlert | None:
        async with self._sessions() as session, session.begin():
            product = await session.get(Product, product_id)
            if product is None:
                return None
            telegram_id = await session.scalar(
                select(User.telegram_id).where(User.id == product.user_id)
            )
            if telegram_id is None:
                return None

            previous_price = product.current_price
            previous_availability = product.is_available
            source_changed = product.price_source != snapshot.source
            decision: AlertDecision | None = None
            if source_changed:
                next_reference = snapshot.price
                next_latched = False
            else:
                decision, next_reference, next_latched = evaluate_alert(
                    threshold_kind=ThresholdKind(product.threshold_kind),
                    threshold_value=product.threshold_value,
                    reference_price=product.reference_price,
                    previous_price=previous_price,
                    current_price=snapshot.price,
                    was_available=previous_availability,
                    is_available=snapshot.available,
                    alert_latched=product.alert_latched,
                )

            changed = (
                previous_price != snapshot.price
                or previous_availability != snapshot.available
                or source_changed
            )
            product.title = snapshot.title[:500]
            product.brand = snapshot.brand[:200] if snapshot.brand else None
            product.current_price = snapshot.price if snapshot.price is not None else previous_price
            product.reference_price = next_reference
            product.lowest_price = _lowest(product.lowest_price, snapshot.price)
            product.basic_price = snapshot.basic_price
            product.is_available = snapshot.available
            product.quantity = snapshot.quantity
            product.price_source = snapshot.source
            product.last_checked_at = snapshot.observed_at
            product.consecutive_errors = 0
            product.last_error = None
            product.alert_latched = next_latched
            if decision is not None:
                product.last_alert_at = snapshot.observed_at
            if changed:
                session.add(
                    PriceHistory(
                        product_id=product.id,
                        price=snapshot.price,
                        basic_price=snapshot.basic_price,
                        is_available=snapshot.available,
                        quantity=snapshot.quantity,
                        source=snapshot.source,
                        observed_at=snapshot.observed_at,
                    )
                )
            if decision is None:
                return None
            outbox = NotificationOutbox(
                event_key=(
                    f"{product.id}:{decision.kind}:{snapshot.observed_at.isoformat()}:"
                    f"{decision.current_price}"
                ),
                telegram_id=int(telegram_id),
                product_id=product.id,
                nm_id=product.nm_id,
                title=product.title,
                canonical_url=product.canonical_url,
                source=snapshot.source,
                kind=decision.kind,
                reference_price=decision.reference_price,
                current_price=decision.current_price,
                drop_amount=decision.drop_amount,
                drop_basis_points=decision.drop_basis_points,
                available_at=snapshot.observed_at,
            )
            session.add(outbox)
            await session.flush()
            return PendingAlert(
                outbox_id=outbox.id,
                telegram_id=int(telegram_id),
                product_id=product.id,
                nm_id=product.nm_id,
                title=product.title,
                canonical_url=product.canonical_url,
                source=snapshot.source,
                decision=decision,
            )

    async def pending_alerts(self, *, limit: int = 100) -> list[PendingAlert]:
        async with self._sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(NotificationOutbox)
                        .where(
                            NotificationOutbox.sent_at.is_(None),
                            NotificationOutbox.available_at <= utcnow(),
                        )
                        .order_by(NotificationOutbox.created_at, NotificationOutbox.id)
                        .limit(limit)
                    )
                ).all()
            )
            return [
                PendingAlert(
                    outbox_id=row.id,
                    telegram_id=row.telegram_id,
                    product_id=row.product_id,
                    nm_id=row.nm_id,
                    title=row.title,
                    canonical_url=row.canonical_url,
                    source=row.source,
                    decision=AlertDecision(
                        kind=row.kind,
                        reference_price=row.reference_price,
                        current_price=row.current_price,
                        drop_amount=row.drop_amount,
                        drop_basis_points=row.drop_basis_points,
                    ),
                    attempts=row.attempts,
                )
                for row in rows
            ]

    async def mark_alert_sent(self, outbox_id: int) -> None:
        async with self._sessions() as session, session.begin():
            row = await session.get(NotificationOutbox, outbox_id)
            if row is not None:
                row.sent_at = utcnow()
                row.last_error = None

    async def discard_alert(self, outbox_id: int, reason: str) -> None:
        async with self._sessions() as session, session.begin():
            row = await session.get(NotificationOutbox, outbox_id)
            if row is not None and row.sent_at is None:
                row.sent_at = utcnow()
                row.last_error = f"discarded: {reason}"[:500]

    async def mark_alert_failed(self, outbox_id: int, error: str, retry_seconds: int) -> None:
        async with self._sessions() as session, session.begin():
            row = await session.get(NotificationOutbox, outbox_id)
            if row is not None and row.sent_at is None:
                row.attempts += 1
                row.last_error = error[:500]
                row.available_at = utcnow() + timedelta(seconds=max(1, retry_seconds))

    async def record_failures(self, product_ids: Sequence[int], error: str) -> None:
        if not product_ids:
            return
        async with self._sessions() as session, session.begin():
            products = (
                await session.scalars(select(Product).where(Product.id.in_(product_ids)))
            ).all()
            for product in products:
                product.consecutive_errors += 1
                product.last_error = error[:500]
                product.last_checked_at = utcnow()

    async def set_state(self, key: str, value: str) -> None:
        async with self._sessions() as session, session.begin():
            state = await session.get(SystemState, key)
            if state is None:
                session.add(SystemState(key=key, value=value))
            else:
                state.value = value

    async def stats(self) -> DatabaseStats:
        async with self._sessions() as session:
            total = int(await session.scalar(select(func.count()).select_from(Product)) or 0)
            active = int(
                await session.scalar(
                    select(func.count()).select_from(Product).where(Product.is_active.is_(True))
                )
                or 0
            )
            users = int(await session.scalar(select(func.count()).select_from(User)) or 0)
            raw_cycle = await session.scalar(
                select(SystemState.value).where(SystemState.key == "last_monitor_cycle")
            )
            pending = int(
                await session.scalar(
                    select(func.count())
                    .select_from(NotificationOutbox)
                    .where(NotificationOutbox.sent_at.is_(None))
                )
                or 0
            )
            cycle = datetime.fromisoformat(raw_cycle) if raw_cycle else None
            return DatabaseStats(total, active, users, cycle, pending)

    async def prune_history(self, days: int) -> int:
        cutoff = utcnow() - timedelta(days=days)
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                delete(PriceHistory).where(PriceHistory.observed_at < cutoff)
            )
            return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def backup_to(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_sqlite_backup, self.path, destination)

    async def integrity_check(self) -> str:
        return await asyncio.to_thread(_sqlite_integrity_check, self.path)

    async def _require_user(self, session: AsyncSession, telegram_id: int) -> User:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            raise RuntimeError("Сначала отправьте боту /start")
        return user

    async def _owned_product(
        self, session: AsyncSession, telegram_id: int, product_id: int
    ) -> Product | None:
        return cast(
            Product | None,
            await session.scalar(
                select(Product)
                .join(User, Product.user_id == User.id)
                .where(Product.id == product_id, User.telegram_id == telegram_id)
            ),
        )


def _lowest(old: int | None, new: int | None) -> int | None:
    if new is None:
        return old
    if old is None:
        return new
    return min(old, new)


def _sqlite_backup(source: Path, destination: Path) -> None:
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as destination_db:
        source_db.backup(destination_db)
        result = destination_db.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError("Резервная копия SQLite не прошла integrity_check")


def _sqlite_integrity_check(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        return str(result[0]) if result else "unknown"


def normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
