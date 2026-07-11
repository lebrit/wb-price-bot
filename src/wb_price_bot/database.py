from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import defaultdict
from collections.abc import Sequence, Set
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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

from .domain import (
    AlertDecision,
    PriceRuleState,
    PriceSnapshot,
    ThresholdKind,
    VariantSelection,
    evaluate_alert,
    utcnow,
)
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
    destination: int
    option_id: int | None
    size_name: str | None
    supplier_id: int | None
    supplier_name: str | None


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
    rule_kind: ThresholdKind | None = None
    rule_value: int | None = None


@dataclass(frozen=True, slots=True)
class NotificationPreferences:
    telegram_id: int
    quiet_start_minute: int | None
    quiet_end_minute: int | None
    daily_digest_enabled: bool
    daily_digest_minute: int
    last_digest_date: str | None


@dataclass(frozen=True, slots=True)
class DigestItem:
    title: str
    canonical_url: str
    current_price: int | None
    first_price: int | None
    is_available: bool


_MIGRATION_COLUMNS: dict[str, dict[str, str]] = {
    "users": {
        "wb_destination": "INTEGER",
        "region_label": "VARCHAR(200)",
        "quiet_start_minute": "INTEGER",
        "quiet_end_minute": "INTEGER",
        "daily_digest_enabled": "BOOLEAN NOT NULL DEFAULT 0",
        "daily_digest_minute": "INTEGER NOT NULL DEFAULT 540",
        "last_digest_date": "VARCHAR(10)",
    },
    "products": {
        "rules_json": "TEXT NOT NULL DEFAULT '[]'",
        "option_id": "BIGINT",
        "size_name": "VARCHAR(100)",
        "supplier_id": "BIGINT",
        "supplier_name": "VARCHAR(200)",
        "folder_name": "VARCHAR(100)",
        "tags_json": "TEXT NOT NULL DEFAULT '[]'",
    },
    "notification_outbox": {
        "rule_kind": "VARCHAR(20)",
        "rule_value": "INTEGER",
    },
}


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
            for table, columns in _MIGRATION_COLUMNS.items():
                existing = {
                    str(row[1])
                    for row in (await connection.exec_driver_sql(f"PRAGMA table_info({table})"))
                }
                for column, definition in columns.items():
                    if column not in existing:
                        await connection.exec_driver_sql(
                            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                        )
        await self._migrate_legacy_rules()

    async def _migrate_legacy_rules(self) -> None:
        async with self._sessions() as session, session.begin():
            products = list((await session.scalars(select(Product))).all())
            for product in products:
                if _load_rules(product.rules_json):
                    continue
                product.rules_json = _dump_rules(
                    [
                        PriceRuleState(
                            id=1,
                            kind=ThresholdKind(product.threshold_kind),
                            value=product.threshold_value,
                            reference_price=product.reference_price,
                            alert_latched=product.alert_latched,
                            last_alert_at=normalize_datetime(product.last_alert_at),
                        )
                    ]
                )

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
        selection: VariantSelection | None = None,
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

                selection = selection or VariantSelection(
                    option_id=snapshot.option_id,
                    size_name=snapshot.size_name,
                    supplier_id=snapshot.supplier_id,
                    supplier_name=snapshot.supplier_name,
                )
                initial_rule = PriceRuleState(
                    id=1,
                    kind=threshold_kind,
                    value=threshold_value,
                    reference_price=snapshot.price,
                )
                product = Product(
                    user_id=user.id,
                    nm_id=snapshot.nm_id,
                    title=snapshot.title[:500],
                    brand=snapshot.brand[:200] if snapshot.brand else None,
                    canonical_url=snapshot.url,
                    threshold_kind=threshold_kind.value,
                    threshold_value=threshold_value,
                    rules_json=_dump_rules([initial_rule]),
                    option_id=selection.option_id,
                    size_name=selection.size_name,
                    supplier_id=selection.supplier_id,
                    supplier_name=selection.supplier_name,
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
        self,
        telegram_id: int,
        *,
        offset: int = 0,
        limit: int = 10,
        folder: str | None = None,
        tag: str | None = None,
    ) -> tuple[list[Product], int]:
        async with self._sessions() as session:
            user_id = await session.scalar(select(User.id).where(User.telegram_id == telegram_id))
            if user_id is None:
                return [], 0
            products = list(
                (
                    await session.scalars(
                        select(Product)
                        .where(Product.user_id == user_id)
                        .order_by(Product.created_at.desc())
                    )
                ).all()
            )
            if folder is not None:
                products = [item for item in products if item.folder_name == folder]
            if tag is not None:
                products = [
                    item
                    for item in products
                    if tag.casefold() in {value.casefold() for value in _load_tags(item.tags_json)}
                ]
            total = len(products)
            return products[offset : offset + limit], total

    async def product_rules(self, telegram_id: int, product_id: int) -> list[PriceRuleState]:
        product = await self.get_product(telegram_id, product_id)
        return _load_rules(product.rules_json) if product is not None else []

    async def add_rule(
        self,
        telegram_id: int,
        product_id: int,
        kind: ThresholdKind,
        value: int,
        *,
        max_rules: int,
    ) -> PriceRuleState:
        async with self._sessions() as session, session.begin():
            product = await self._owned_product(session, telegram_id, product_id)
            if product is None:
                raise RuntimeError("Товар не найден")
            rules = _load_rules(product.rules_json)
            if len(rules) >= max_rules:
                raise ProductLimitError(f"Достигнут лимит: {max_rules} правил")
            rule = PriceRuleState(
                id=max((item.id for item in rules), default=0) + 1,
                kind=kind,
                value=value,
                reference_price=product.current_price,
            )
            rules.append(rule)
            product.rules_json = _dump_rules(rules)
            return rule

    async def delete_rule(self, telegram_id: int, product_id: int, rule_id: int) -> bool:
        async with self._sessions() as session, session.begin():
            product = await self._owned_product(session, telegram_id, product_id)
            if product is None:
                return False
            rules = _load_rules(product.rules_json)
            if len(rules) <= 1:
                raise RuntimeError("У товара должно остаться хотя бы одно правило")
            filtered = [item for item in rules if item.id != rule_id]
            if len(filtered) == len(rules):
                return False
            product.rules_json = _dump_rules(filtered)
            return True

    async def organize_product(
        self,
        telegram_id: int,
        product_id: int,
        *,
        folder: str | None,
        tags: list[str],
    ) -> bool:
        async with self._sessions() as session, session.begin():
            product = await self._owned_product(session, telegram_id, product_id)
            if product is None:
                return False
            product.folder_name = folder[:100] if folder else None
            product.tags_json = _dump_tags(tags)
            return True

    async def set_variant(
        self,
        telegram_id: int,
        product_id: int,
        snapshot: PriceSnapshot,
    ) -> bool:
        async with self._sessions() as session, session.begin():
            product = await self._owned_product(session, telegram_id, product_id)
            if product is None:
                return False
            product.option_id = snapshot.option_id
            product.size_name = snapshot.size_name
            product.supplier_id = snapshot.supplier_id
            product.supplier_name = snapshot.supplier_name
            product.current_price = snapshot.price
            product.basic_price = snapshot.basic_price
            product.is_available = snapshot.available
            product.quantity = snapshot.quantity
            product.price_source = "context_reset"
            rules = _load_rules(product.rules_json)
            for rule in rules:
                rule.reference_price = None
                rule.alert_latched = False
            product.rules_json = _dump_rules(rules)
            return True

    async def organization_summary(self, telegram_id: int) -> tuple[dict[str, int], dict[str, int]]:
        products, _ = await self.list_products(telegram_id, limit=10_000)
        folders: dict[str, int] = {}
        tags: dict[str, int] = {}
        for product in products:
            if product.folder_name:
                folders[product.folder_name] = folders.get(product.folder_name, 0) + 1
            for tag in _load_tags(product.tags_json):
                tags[tag] = tags.get(tag, 0) + 1
        return folders, tags

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

    async def history_since(
        self, telegram_id: int, product_id: int, *, since: datetime
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
                        .where(
                            PriceHistory.product_id == product_id,
                            PriceHistory.observed_at >= since,
                        )
                        .order_by(PriceHistory.observed_at.asc())
                    )
                ).all()
            )

    async def set_region(self, telegram_id: int, *, destination: int, label: str) -> None:
        async with self._sessions() as session, session.begin():
            user = await self._require_user(session, telegram_id)
            user.wb_destination = destination
            user.region_label = label[:200]
            for product in (
                await session.scalars(select(Product).where(Product.user_id == user.id))
            ).all():
                rules = _load_rules(product.rules_json)
                for rule in rules:
                    rule.reference_price = None
                    rule.alert_latched = False
                product.rules_json = _dump_rules(rules)
                product.price_source = "context_reset"

    async def set_quiet_hours(
        self, telegram_id: int, start_minute: int | None, end_minute: int | None
    ) -> None:
        async with self._sessions() as session, session.begin():
            user = await self._require_user(session, telegram_id)
            user.quiet_start_minute = start_minute
            user.quiet_end_minute = end_minute

    async def set_digest(
        self, telegram_id: int, *, enabled: bool, minute: int | None = None
    ) -> None:
        async with self._sessions() as session, session.begin():
            user = await self._require_user(session, telegram_id)
            user.daily_digest_enabled = enabled
            if minute is not None:
                user.daily_digest_minute = minute

    async def notification_preferences(
        self, allowed_telegram_ids: Set[int]
    ) -> dict[int, NotificationPreferences]:
        if not allowed_telegram_ids:
            return {}
        async with self._sessions() as session:
            users = list(
                (
                    await session.scalars(
                        select(User).where(User.telegram_id.in_(allowed_telegram_ids))
                    )
                ).all()
            )
            return {
                user.telegram_id: NotificationPreferences(
                    telegram_id=user.telegram_id,
                    quiet_start_minute=user.quiet_start_minute,
                    quiet_end_minute=user.quiet_end_minute,
                    daily_digest_enabled=user.daily_digest_enabled,
                    daily_digest_minute=user.daily_digest_minute,
                    last_digest_date=user.last_digest_date,
                )
                for user in users
            }

    async def mark_digest_sent(self, telegram_id: int, value: date) -> None:
        async with self._sessions() as session, session.begin():
            user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user is not None:
                user.last_digest_date = value.isoformat()

    async def digest_items(self, telegram_id: int, *, since: datetime) -> list[DigestItem]:
        products, _ = await self.list_products(telegram_id, limit=10_000)
        result: list[DigestItem] = []
        async with self._sessions() as session:
            for product in products:
                first_price = await session.scalar(
                    select(PriceHistory.price)
                    .where(
                        PriceHistory.product_id == product.id,
                        PriceHistory.observed_at >= since,
                        PriceHistory.price.is_not(None),
                    )
                    .order_by(PriceHistory.observed_at.asc())
                    .limit(1)
                )
                result.append(
                    DigestItem(
                        title=product.title,
                        canonical_url=product.canonical_url,
                        current_price=product.current_price,
                        first_price=first_price,
                        is_available=product.is_available,
                    )
                )
        return result

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
                rules = _load_rules(product.rules_json)
                for rule in rules:
                    rule.reference_price = None
                    rule.alert_latched = False
                product.rules_json = _dump_rules(rules)
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

    async def active_targets(
        self, allowed_telegram_ids: Set[int], default_destination: int
    ) -> list[WatchTarget]:
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
                        User.wb_destination,
                        Product.option_id,
                        Product.size_name,
                        Product.supplier_id,
                        Product.supplier_name,
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
            return [
                WatchTarget(
                    product_id=row[0],
                    user_id=row[1],
                    telegram_id=row[2],
                    nm_id=row[3],
                    encrypted_session=row[4],
                    account_status=row[5],
                    destination=row[6] if row[6] is not None else default_destination,
                    option_id=row[7],
                    size_name=row[8],
                    supplier_id=row[9],
                    supplier_name=row[10],
                )
                for row in rows
            ]

    async def apply_snapshot(self, product_id: int, snapshot: PriceSnapshot) -> list[PendingAlert]:
        async with self._sessions() as session, session.begin():
            product = await session.get(Product, product_id)
            if product is None:
                return []
            telegram_id = await session.scalar(
                select(User.telegram_id).where(User.id == product.user_id)
            )
            if telegram_id is None:
                return []

            previous_price = product.current_price
            previous_availability = product.is_available
            source_changed = product.price_source != snapshot.source
            rules = _load_rules(product.rules_json)
            decisions: list[tuple[PriceRuleState, AlertDecision]] = []
            restock_created = False
            if source_changed:
                for rule in rules:
                    rule.reference_price = snapshot.price
                    rule.alert_latched = False
            else:
                for rule in rules:
                    if not rule.is_active:
                        continue
                    decision, next_reference, next_latched = evaluate_alert(
                        threshold_kind=rule.kind,
                        threshold_value=rule.value,
                        reference_price=rule.reference_price,
                        previous_price=previous_price,
                        current_price=snapshot.price,
                        was_available=previous_availability,
                        is_available=snapshot.available,
                        alert_latched=rule.alert_latched,
                    )
                    rule.reference_price = next_reference
                    rule.alert_latched = next_latched
                    if decision is not None:
                        rule.last_alert_at = snapshot.observed_at
                        if decision.kind == "back_in_stock":
                            if restock_created:
                                continue
                            restock_created = True
                        decisions.append((rule, decision))

            changed = (
                previous_price != snapshot.price
                or previous_availability != snapshot.available
                or source_changed
            )
            product.title = snapshot.title[:500]
            product.brand = snapshot.brand[:200] if snapshot.brand else None
            product.current_price = snapshot.price if snapshot.price is not None else previous_price
            if rules:
                product.reference_price = rules[0].reference_price
                product.alert_latched = rules[0].alert_latched
                product.threshold_kind = rules[0].kind.value
                product.threshold_value = rules[0].value
            product.rules_json = _dump_rules(rules)
            product.lowest_price = _lowest(product.lowest_price, snapshot.price)
            product.basic_price = snapshot.basic_price
            product.is_available = snapshot.available
            product.quantity = snapshot.quantity
            product.price_source = snapshot.source
            product.last_checked_at = snapshot.observed_at
            product.consecutive_errors = 0
            product.last_error = None
            if decisions:
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
            alerts: list[PendingAlert] = []
            for rule, decision in decisions:
                outbox = NotificationOutbox(
                    event_key=(
                        f"{product.id}:{rule.id}:{decision.kind}:"
                        f"{snapshot.observed_at.isoformat()}:{decision.current_price}"
                    ),
                    telegram_id=int(telegram_id),
                    product_id=product.id,
                    nm_id=product.nm_id,
                    title=product.title,
                    canonical_url=product.canonical_url,
                    source=snapshot.source,
                    kind=decision.kind,
                    rule_kind=rule.kind.value,
                    rule_value=rule.value,
                    reference_price=decision.reference_price,
                    current_price=decision.current_price,
                    drop_amount=decision.drop_amount,
                    drop_basis_points=decision.drop_basis_points,
                    available_at=snapshot.observed_at,
                )
                session.add(outbox)
                await session.flush()
                alerts.append(
                    PendingAlert(
                        outbox_id=outbox.id,
                        telegram_id=int(telegram_id),
                        product_id=product.id,
                        nm_id=product.nm_id,
                        title=product.title,
                        canonical_url=product.canonical_url,
                        source=snapshot.source,
                        decision=decision,
                        rule_kind=rule.kind,
                        rule_value=rule.value,
                    )
                )
            return alerts

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
                    rule_kind=ThresholdKind(row.rule_kind) if row.rule_kind else None,
                    rule_value=row.rule_value,
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


def _load_rules(raw: str | None) -> list[PriceRuleState]:
    try:
        payload = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, list):
        return []
    result: list[PriceRuleState] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            last_alert = item.get("last_alert_at")
            result.append(
                PriceRuleState(
                    id=int(item["id"]),
                    kind=ThresholdKind(str(item["kind"])),
                    value=int(item["value"]),
                    reference_price=(
                        int(item["reference_price"])
                        if item.get("reference_price") is not None
                        else None
                    ),
                    alert_latched=bool(item.get("alert_latched", False)),
                    is_active=bool(item.get("is_active", True)),
                    last_alert_at=(datetime.fromisoformat(str(last_alert)) if last_alert else None),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _dump_rules(rules: Sequence[PriceRuleState]) -> str:
    return json.dumps(
        [
            {
                "id": item.id,
                "kind": item.kind.value,
                "value": item.value,
                "reference_price": item.reference_price,
                "alert_latched": item.alert_latched,
                "is_active": item.is_active,
                "last_alert_at": item.last_alert_at.isoformat() if item.last_alert_at else None,
            }
            for item in rules
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _load_tags(raw: str | None) -> list[str]:
    try:
        payload = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return (
        [str(item) for item in payload if isinstance(item, str)]
        if isinstance(payload, list)
        else []
    )


def _dump_tags(tags: Sequence[str]) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = raw.strip().lstrip("#")[:50]
        key = tag.casefold()
        if tag and key not in seen:
            result.append(tag)
            seen.add(key)
        if len(result) >= 20:
            break
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


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
