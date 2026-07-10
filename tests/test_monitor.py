from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from wb_price_bot.config import Settings
from wb_price_bot.database import Database
from wb_price_bot.domain import ThresholdKind
from wb_price_bot.monitor import PriceMonitor
from wb_price_bot.security import SessionCipher
from wb_price_bot.wildberries import AccountProviderError, AccountSessionError, FetchResult

from .conftest import make_snapshot


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **_: Any) -> None:
        self.messages.append((chat_id, text))


class FakePublicClient:
    def __init__(self, price: int) -> None:
        self.price = price
        self.calls: list[list[int]] = []

    async def fetch_many(self, nm_ids: list[int], **_: Any) -> FetchResult:
        self.calls.append(nm_ids)
        return FetchResult(
            {nm_id: make_snapshot(nm_id=nm_id, price=self.price) for nm_id in nm_ids}
        )


class FailingAccountClient:
    async def fetch_many(self, nm_ids: list[int], session_state: str) -> FetchResult:
        raise AccountSessionError("session expired")


class TransientFailingAccountClient:
    async def fetch_many(self, nm_ids: list[int], session_state: str) -> FetchResult:
        raise AccountProviderError("chromium unavailable")


class PartiallyFailingAccountClient:
    async def fetch_many(self, nm_ids: list[int], session_state: str) -> FetchResult:
        return FetchResult(
            {nm_ids[0]: make_snapshot(nm_id=nm_ids[0], price=90_000, source="account_browser")},
            errors={nm_ids[1]: "visible price not found"},
        )


@pytest.mark.asyncio
async def test_monitor_deduplicates_public_product_and_sends_alert(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    for telegram_id in (1001, 1002):
        await database.ensure_user(telegram_id, None, "User", is_admin=True)
        await database.add_product(
            telegram_id=telegram_id,
            snapshot=make_snapshot(price=100_000),
            threshold_kind=ThresholdKind.PERCENT,
            threshold_value=1000,
            max_products=20,
        )
    bot = FakeBot()
    public = FakePublicClient(89_000)
    monitor = PriceMonitor(
        settings=settings,
        database=database,
        bot=bot,  # type: ignore[arg-type]
        public_client=public,  # type: ignore[arg-type]
        account_client=FailingAccountClient(),  # type: ignore[arg-type]
        cipher=SessionCipher(settings.session_encryption_key),
    )
    await monitor.check_all()
    assert public.calls == [[28436956]]
    assert len(bot.messages) == 2
    assert all("Цена снизилась" in text for _, text in bot.messages)
    await database.close()


@pytest.mark.asyncio
async def test_account_failure_is_not_silently_retried_as_public(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    user = await database.ensure_user(1001, None, "Owner", is_admin=True)
    await database.add_product(
        telegram_id=1001,
        snapshot=make_snapshot(price=100_000),
        threshold_kind=ThresholdKind.AMOUNT,
        threshold_value=5000,
        max_products=20,
    )
    cipher = SessionCipher(settings.session_encryption_key)
    await database.save_wb_account(1001, cipher.encrypt('{"cookies":[],"origins":[]}'))
    bot = FakeBot()
    public = FakePublicClient(50_000)
    monitor = PriceMonitor(
        settings=settings,
        database=database,
        bot=bot,  # type: ignore[arg-type]
        public_client=public,  # type: ignore[arg-type]
        account_client=FailingAccountClient(),  # type: ignore[arg-type]
        cipher=cipher,
    )
    await monitor.check_all()
    assert public.calls == []
    account = await database.get_wb_account(1001)
    assert account is not None and account.status == "error"
    assert account.user_id == user.id
    assert len(bot.messages) == 1
    assert "приостановлена" in bot.messages[0][1]
    await monitor.check_all()
    assert public.calls == []
    assert len(bot.messages) == 1
    await database.close()


@pytest.mark.asyncio
async def test_transient_account_provider_failure_does_not_disable_session(
    settings: Settings,
) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_user(1001, None, "Owner", is_admin=True)
    await database.add_product(
        telegram_id=1001,
        snapshot=make_snapshot(),
        threshold_kind=ThresholdKind.AMOUNT,
        threshold_value=5000,
        max_products=20,
    )
    cipher = SessionCipher(settings.session_encryption_key)
    await database.save_wb_account(1001, cipher.encrypt('{"cookies":[],"origins":[]}'))
    public = FakePublicClient(50_000)
    monitor = PriceMonitor(
        settings=settings,
        database=database,
        bot=FakeBot(),  # type: ignore[arg-type]
        public_client=public,  # type: ignore[arg-type]
        account_client=TransientFailingAccountClient(),  # type: ignore[arg-type]
        cipher=cipher,
    )

    await monitor.check_all()
    account = await database.get_wb_account(1001)
    assert account is not None and account.status == "active"
    assert public.calls == []
    await database.close()


@pytest.mark.asyncio
async def test_account_product_failure_does_not_block_other_products(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_user(1001, None, "Owner", is_admin=True)
    products = []
    for nm_id in (28_436_956, 28_436_957):
        products.append(
            await database.add_product(
                telegram_id=1001,
                snapshot=make_snapshot(nm_id=nm_id),
                threshold_kind=ThresholdKind.AMOUNT,
                threshold_value=5000,
                max_products=20,
            )
        )
    cipher = SessionCipher(settings.session_encryption_key)
    await database.save_wb_account(1001, cipher.encrypt('{"cookies":[],"origins":[]}'))
    monitor = PriceMonitor(
        settings=settings,
        database=database,
        bot=FakeBot(),  # type: ignore[arg-type]
        public_client=FakePublicClient(50_000),  # type: ignore[arg-type]
        account_client=PartiallyFailingAccountClient(),  # type: ignore[arg-type]
        cipher=cipher,
    )

    await monitor.check_all()
    updated = await database.get_product(1001, products[0].id)
    failed = await database.get_product(1001, products[1].id)
    account = await database.get_wb_account(1001)
    assert updated is not None and updated.current_price == 90_000
    assert failed is not None and failed.consecutive_errors == 1
    assert failed.last_error == "visible price not found"
    assert account is not None and account.status == "active"
    await database.close()


@pytest.mark.asyncio
async def test_removed_allowlist_user_does_not_receive_queued_alert(settings: Settings) -> None:
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
    assert await database.apply_snapshot(product.id, make_snapshot(price=90_000)) is not None
    bot = FakeBot()
    monitor = PriceMonitor(
        settings=replace(settings, allowed_users=frozenset({1002})),
        database=database,
        bot=bot,  # type: ignore[arg-type]
        public_client=FakePublicClient(50_000),  # type: ignore[arg-type]
        account_client=FailingAccountClient(),  # type: ignore[arg-type]
        cipher=SessionCipher(settings.session_encryption_key),
    )

    await monitor.check_all()
    assert bot.messages == []
    assert (await database.stats()).alerts_pending == 0
    await database.close()
