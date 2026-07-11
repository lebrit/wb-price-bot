from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from wb_price_bot.config import Settings
from wb_price_bot.database import (
    Database,
    ProductAlreadyExistsError,
    ProductLimitError,
    auth_pairing_code,
)
from wb_price_bot.domain import PriceSnapshot, ThresholdKind, utcnow

from .conftest import make_snapshot


@pytest.mark.asyncio
async def test_initialize_migrates_v010_database_and_is_idempotent(settings: Settings) -> None:
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY, telegram_id BIGINT NOT NULL UNIQUE,
                username VARCHAR(64), display_name VARCHAR(128) NOT NULL,
                is_admin BOOLEAN NOT NULL, is_enabled BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
            );
            CREATE TABLE products (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, nm_id BIGINT NOT NULL,
                title VARCHAR(500) NOT NULL, brand VARCHAR(200), canonical_url VARCHAR(500) NOT NULL,
                threshold_kind VARCHAR(20) NOT NULL, threshold_value INTEGER NOT NULL,
                is_active BOOLEAN NOT NULL, is_available BOOLEAN NOT NULL,
                alert_latched BOOLEAN NOT NULL, current_price INTEGER, reference_price INTEGER,
                lowest_price INTEGER, basic_price INTEGER, price_source VARCHAR(32) NOT NULL,
                quantity INTEGER NOT NULL, consecutive_errors INTEGER NOT NULL,
                last_error VARCHAR(500), last_checked_at DATETIME, last_alert_at DATETIME,
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            INSERT INTO users VALUES
                (1, 1001, 'owner', 'Owner', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
            INSERT INTO products VALUES
                (1, 1, 28436956, 'Legacy', 'WB', 'https://www.wildberries.ru/catalog/28436956/detail.aspx',
                 'percent', 1000, 1, 1, 0, 100000, 100000, 100000, 150000,
                 'public_api', 5, 0, NULL, CURRENT_TIMESTAMP, NULL,
                 CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
            """
        )

    database = Database(settings.database_path)
    await database.initialize()
    await database.initialize()
    product = await database.get_product(1001, 1)
    user = await database.get_user(1001)
    rules = await database.product_rules(1001, 1)

    assert product is not None
    assert product.rules_json != "[]"
    assert product.tags_json == "[]"
    assert user is not None and user.access_status == "approved"
    assert len(rules) == 1
    assert rules[0].kind is ThresholdKind.PERCENT
    assert rules[0].value == 1000
    assert rules[0].reference_price == 100_000
    await database.close()


@pytest.mark.asyncio
async def test_user_approval_and_auth_sessions_are_isolated(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_user(1001, "admin", "Admin", is_admin=True)
    pending = await database.ensure_user(
        2002,
        "guest",
        "Guest",
        is_admin=False,
        auto_approve=False,
    )
    assert pending.access_status == "pending"
    assert await database.approved_telegram_ids(frozenset({1001})) == {1001}
    access_stats = await database.admin_access_stats()
    assert access_stats.users_pending == 1
    assert access_stats.users_approved == 1

    reviewed = await database.review_user_access(
        1001,
        2002,
        approve=True,
        configured_admins=frozenset({1001}),
    )
    assert reviewed is not None and reviewed.access_status == "approved"
    assert await database.approved_telegram_ids(frozenset({1001})) == {1001, 2002}

    first = await database.create_auth_session(2002, 600)
    pairing_code = auth_pairing_code(first.id)
    assert len(pairing_code) == 10
    assert await database.get_auth_session(first.id, 1001) is None
    assert await database.activate_auth_session(first.id, 1001) is False
    assert await database.queue_auth_session(first.id, 2002) is True
    assert await database.activate_auth_session(first.id, 2002) is True
    access_stats = await database.admin_access_stats()
    assert access_stats.auth_active == 1
    second = await database.create_auth_session(2002, 600)
    cancelled = await database.get_auth_session(first.id, 2002)
    assert cancelled is not None and cancelled.status == "cancelled"
    assert await database.queue_auth_session(first.id, 2002) is False
    assert await database.complete_auth_session(first.id, 2002, "encrypted-old") is False
    assert await database.get_wb_account(2002) is None
    assert await database.activate_auth_session(second.id, 2002) is True
    assert await database.complete_auth_session(second.id, 2002, "encrypted-new") is True
    account = await database.get_wb_account(2002)
    completed = await database.get_auth_session(second.id, 2002)
    assert account is not None and account.encrypted_session == "encrypted-new"
    assert completed is not None and completed.status == "succeeded"
    assert (
        await database.set_auth_session_status(second.id, "failed", expected_statuses=("active",))
        is False
    )

    expired = await database.create_auth_session(2002, -1)
    assert await database.activate_auth_session(expired.id, 2002) is False
    expired_after = await database.get_auth_session(expired.id, 2002)
    assert expired_after is not None and expired_after.status == "expired"

    with pytest.raises(PermissionError, match="администратор"):
        await database.review_user_access(
            1001,
            2002,
            approve=False,
            configured_admins=frozenset({9999}),
        )
    await database.close()


@pytest.mark.asyncio
async def test_connector_pairing_code_is_one_time_and_user_bound(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_user(1001, "owner", "Owner", is_admin=True)
    auth_session = await database.create_auth_session(1001, 600)
    code = auth_pairing_code(auth_session.id)
    assert await database.activate_connector_session("BAD-CODE") is None
    assert await database.activate_connector_session(code) == (auth_session.id, 1001)
    assert await database.activate_connector_session(code) is None
    await database.close()


@pytest.mark.asyncio
async def test_database_product_lifecycle_and_alerts(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_user(1001, "owner", "Owner", is_admin=True)
    product = await database.add_product(
        telegram_id=1001,
        snapshot=make_snapshot(price=100_000),
        threshold_kind=ThresholdKind.PERCENT,
        threshold_value=1000,
        max_products=20,
    )
    with pytest.raises(ProductAlreadyExistsError):
        await database.add_product(
            telegram_id=1001,
            snapshot=make_snapshot(price=100_000),
            threshold_kind=ThresholdKind.PERCENT,
            threshold_value=1000,
            max_products=20,
        )

    no_alert = await database.apply_snapshot(product.id, make_snapshot(price=95_000))
    assert no_alert == []
    alerts = await database.apply_snapshot(product.id, make_snapshot(price=89_000))
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.decision.kind == "price_drop"
    assert alert.decision.reference_price == 100_000

    updated = await database.get_product(1001, product.id)
    assert updated is not None
    assert updated.reference_price == 89_000
    assert updated.lowest_price == 89_000

    source_change = await database.apply_snapshot(
        product.id, make_snapshot(price=80_000, source="account_browser")
    )
    assert source_change == []
    updated = await database.get_product(1001, product.id)
    assert updated is not None
    assert updated.reference_price == 80_000
    assert updated.price_source == "account_browser"

    history = await database.recent_history(1001, product.id, limit=20)
    assert [row.price for row in history] == [80_000, 89_000, 95_000, 100_000]
    assert await database.delete_product(1002, product.id) is False
    assert await database.delete_product(1001, product.id) is True
    await database.close()


@pytest.mark.asyncio
async def test_out_of_stock_never_overwrites_known_price(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_user(1001, None, "Owner", is_admin=True)
    product = await database.add_product(
        telegram_id=1001,
        snapshot=make_snapshot(price=100_000),
        threshold_kind=ThresholdKind.AMOUNT,
        threshold_value=5000,
        max_products=20,
    )
    snapshot = make_snapshot(price=None, available=False, quantity=0)
    assert await database.apply_snapshot(product.id, snapshot) == []
    updated = await database.get_product(1001, product.id)
    assert updated is not None
    assert updated.current_price == 100_000
    assert updated.is_available is False
    await database.close()


@pytest.mark.asyncio
async def test_backup_is_consistent(settings: Settings, tmp_path: Path) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_user(1001, None, "Owner", is_admin=True)
    destination = tmp_path / "backup" / "copy.sqlite3"
    await database.backup_to(destination)
    assert destination.exists()
    assert await database.integrity_check() == "ok"
    backup_db = Database(destination)
    await backup_db.initialize()
    assert await backup_db.integrity_check() == "ok"
    await backup_db.close()
    await database.close()


@pytest.mark.asyncio
async def test_product_limit_is_atomic_for_concurrent_adds(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_user(1001, None, "Owner", is_admin=True)

    async def add(nm_id: int) -> object:
        return await database.add_product(
            telegram_id=1001,
            snapshot=make_snapshot(nm_id=nm_id),
            threshold_kind=ThresholdKind.PERCENT,
            threshold_value=1000,
            max_products=1,
        )

    results = await asyncio.gather(
        *(add(28_436_956 + index) for index in range(5)), return_exceptions=True
    )
    products, total = await database.list_products(1001)
    assert total == len(products) == 1
    assert sum(isinstance(item, ProductLimitError) for item in results) == 4
    await database.close()


@pytest.mark.asyncio
async def test_alert_is_persisted_until_marked_sent(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_user(1001, None, "Owner", is_admin=True)
    product = await database.add_product(
        telegram_id=1001,
        snapshot=make_snapshot(price=100_000),
        threshold_kind=ThresholdKind.AMOUNT,
        threshold_value=5000,
        max_products=20,
    )

    alerts = await database.apply_snapshot(product.id, make_snapshot(price=90_000))
    assert len(alerts) == 1
    alert = alerts[0]
    pending = await database.pending_alerts()
    assert [item.outbox_id for item in pending] == [alert.outbox_id]
    assert (await database.stats()).alerts_pending == 1

    await database.mark_alert_sent(alert.outbox_id)
    assert await database.pending_alerts() == []
    assert (await database.stats()).alerts_pending == 0
    await database.close()


@pytest.mark.asyncio
async def test_deleting_product_discards_pending_alert(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_user(1001, None, "Owner", is_admin=True)
    product = await database.add_product(
        telegram_id=1001,
        snapshot=make_snapshot(price=100_000),
        threshold_kind=ThresholdKind.AMOUNT,
        threshold_value=5000,
        max_products=20,
    )
    assert await database.apply_snapshot(product.id, make_snapshot(price=90_000))
    assert (await database.stats()).alerts_pending == 1

    assert await database.delete_product(1001, product.id) is True
    assert await database.pending_alerts() == []
    assert (await database.stats()).alerts_pending == 0
    await database.close()


@pytest.mark.asyncio
async def test_replacing_account_resets_price_context(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_user(1001, None, "Owner", is_admin=True)
    product = await database.add_product(
        telegram_id=1001,
        snapshot=make_snapshot(price=100_000, source="account_browser"),
        threshold_kind=ThresholdKind.PERCENT,
        threshold_value=1000,
        max_products=20,
    )

    await database.save_wb_account(1001, "encrypted-session-a")
    await database.save_wb_account(1001, "encrypted-session-b")
    updated = await database.get_product(1001, product.id)
    assert updated is not None
    assert updated.reference_price is None
    assert updated.alert_latched is False
    assert updated.price_source == "context_reset"
    await database.close()


@pytest.mark.asyncio
async def test_active_targets_respect_current_allowlist(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    for telegram_id in (1001, 1002):
        await database.ensure_user(telegram_id, None, "Owner", is_admin=True)
        await database.add_product(
            telegram_id=telegram_id,
            snapshot=make_snapshot(),
            threshold_kind=ThresholdKind.PERCENT,
            threshold_value=1000,
            max_products=20,
        )

    targets = await database.active_targets({1002}, settings.wb_destination)
    assert [item.telegram_id for item in targets] == [1002]
    await database.close()


@pytest.mark.asyncio
async def test_multiple_rules_create_independent_alerts(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_user(1001, None, "Owner", is_admin=True)
    product = await database.add_product(
        telegram_id=1001,
        snapshot=make_snapshot(price=100_000),
        threshold_kind=ThresholdKind.PERCENT,
        threshold_value=1000,
        max_products=20,
    )
    await database.add_rule(1001, product.id, ThresholdKind.AMOUNT, 5000, max_rules=10)

    alerts = await database.apply_snapshot(product.id, make_snapshot(price=90_000))
    assert len(alerts) == 2
    assert {item.rule_kind for item in alerts} == {
        ThresholdKind.PERCENT,
        ThresholdKind.AMOUNT,
    }
    assert len(await database.pending_alerts()) == 2
    await database.close()


@pytest.mark.asyncio
async def test_region_preferences_and_organization_are_persisted(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_user(1001, None, "Owner", is_admin=True)
    product = await database.add_product(
        telegram_id=1001,
        snapshot=make_snapshot(price=100_000),
        threshold_kind=ThresholdKind.TARGET,
        threshold_value=80_000,
        max_products=20,
    )
    await database.set_region(1001, destination=-123, label="Test region")
    await database.set_quiet_hours(1001, 1320, 480)
    await database.set_digest(1001, enabled=True, minute=600)
    await database.organize_product(
        1001, product.id, folder="Подарки", tags=["Дом", "скидки", "дом"]
    )

    user = await database.get_user(1001)
    updated = await database.get_product(1001, product.id)
    preferences = await database.notification_preferences({1001})
    assert user is not None and user.wb_destination == -123
    assert updated is not None and updated.price_source == "context_reset"
    assert updated.folder_name == "Подарки"
    assert updated.tags_json == '["Дом","скидки"]'
    assert (await database.product_rules(1001, product.id))[0].reference_price is None
    assert preferences[1001].quiet_start_minute == 1320
    assert preferences[1001].daily_digest_enabled is True
    await database.close()


def shifted_snapshot(snapshot: PriceSnapshot, *, minutes: int) -> PriceSnapshot:
    return PriceSnapshot(
        nm_id=snapshot.nm_id,
        title=snapshot.title,
        brand=snapshot.brand,
        price=snapshot.price,
        basic_price=snapshot.basic_price,
        available=snapshot.available,
        quantity=snapshot.quantity,
        source=snapshot.source,
        observed_at=utcnow() + timedelta(minutes=minutes),
    )
