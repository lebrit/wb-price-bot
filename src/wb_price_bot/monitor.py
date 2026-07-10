from __future__ import annotations

import asyncio
import html
import logging
import random
from collections import defaultdict
from datetime import date, datetime
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from .config import Settings
from .database import Database, PendingAlert, WatchTarget, normalize_datetime
from .domain import PriceSnapshot, format_money, utcnow
from .security import SessionCipher
from .wildberries import (
    AccountProviderError,
    AccountSessionError,
    AccountWildberriesClient,
    PublicWildberriesClient,
    WildberriesError,
)

logger = logging.getLogger(__name__)


class PriceMonitor:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        bot: Bot,
        public_client: PublicWildberriesClient,
        account_client: AccountWildberriesClient,
        cipher: SessionCipher,
    ) -> None:
        self._settings = settings
        self._database = database
        self._bot = bot
        self._public = public_client
        self._account = account_client
        self._cipher = cipher
        self._stop = asyncio.Event()
        self._cycle_lock = asyncio.Lock()
        self._last_prune_date: date | None = None
        self._last_chat_send: dict[int, float] = {}

    async def run(self) -> None:
        logger.info("Мониторинг цен запущен")
        while not self._stop.is_set():
            started = utcnow()
            try:
                await self.check_all()
                await self._database.set_state("last_monitor_cycle", utcnow().isoformat())
                await self._prune_if_needed()
            except Exception:
                logger.exception("Непредвиденная ошибка цикла мониторинга")
            elapsed = max(0.0, (utcnow() - started).total_seconds())
            jitter = random.uniform(
                -self._settings.check_jitter_seconds,
                self._settings.check_jitter_seconds,
            )
            delay = max(60.0, self._settings.check_interval_seconds + jitter - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass
        logger.info("Мониторинг цен остановлен")

    def stop(self) -> None:
        self._stop.set()

    async def check_all(self) -> None:
        async with self._cycle_lock:
            targets = await self._database.active_targets(self._settings.allowed_users)
            public_targets = [item for item in targets if item.account_status is None]
            if public_targets:
                await self._check_public(public_targets)

            account_groups: dict[int, list[WatchTarget]] = defaultdict(list)
            for target in targets:
                if target.account_status is not None:
                    account_groups[target.user_id].append(target)
            for group in account_groups.values():
                await self._check_account(group)
            await self.drain_outbox()

    async def check_product(self, telegram_id: int, product_id: int) -> PriceSnapshot:
        async with self._cycle_lock:
            product = await self._database.get_product(telegram_id, product_id)
            if product is None:
                raise WildberriesError("Товар не найден")
            account = await self._database.get_wb_account(telegram_id)
            if account is not None:
                if account.status != "active":
                    raise AccountSessionError(
                        "WB-сессия требует обновления; публичная цена не подставлена"
                    )
                try:
                    state = self._cipher.decrypt(account.encrypted_session)
                    result = await self._account.fetch_many([product.nm_id], state)
                except (AccountSessionError, ValueError) as exc:
                    await self._database.set_account_result(
                        product.user_id, success=False, error=str(exc)
                    )
                    raise
                if result.refreshed_session:
                    await self._database.refresh_wb_account_session(
                        product.user_id, self._cipher.encrypt(result.refreshed_session)
                    )
                else:
                    await self._database.set_account_result(product.user_id, success=True)
            else:
                result = await self._public.fetch_many([product.nm_id])
            snapshot = result.products.get(product.nm_id)
            if snapshot is None:
                product_error = result.errors.get(product.nm_id)
                if product_error:
                    raise WildberriesError(product_error)
                raise WildberriesError("Wildberries не вернул данные этого товара")
            await self._database.apply_snapshot(product.id, snapshot)
            await self.drain_outbox()
            return snapshot

    async def _check_public(self, targets: list[WatchTarget]) -> None:
        ids = list(dict.fromkeys(item.nm_id for item in targets))
        try:
            result = await self._public.fetch_many(ids)
        except WildberriesError as exc:
            logger.warning("Публичная проверка WB не выполнена: %s", exc)
            await self._database.record_failures([item.product_id for item in targets], str(exc))
            return
        await self._apply_result(targets, result.products)

    async def _check_account(self, targets: list[WatchTarget]) -> None:
        first = targets[0]
        if first.account_status != "active":
            return
        assert first.encrypted_session is not None
        try:
            state = self._cipher.decrypt(first.encrypted_session)
            result = await self._account.fetch_many(
                list(dict.fromkeys(item.nm_id for item in targets)), state
            )
            if result.refreshed_session:
                await self._database.refresh_wb_account_session(
                    first.user_id, self._cipher.encrypt(result.refreshed_session)
                )
            else:
                await self._database.set_account_result(first.user_id, success=True)
        except (AccountSessionError, ValueError) as exc:
            message = str(exc)
            logger.warning("Персональная сессия WB пользователя %s: %s", first.user_id, message)
            await self._database.set_account_result(first.user_id, success=False, error=message)
            await self._database.record_failures(
                [item.product_id for item in targets], f"Персональная цена: {message}"
            )
            await self._send_account_error(first.telegram_id, message)
            return
        except (AccountProviderError, WildberriesError) as exc:
            logger.warning(
                "Временная ошибка account-provider пользователя %s: %s", first.user_id, exc
            )
            await self._database.record_failures(
                [item.product_id for item in targets], f"Персональная цена: {exc}"
            )
            return
        await self._apply_result(targets, result.products, result.errors)

    async def _apply_result(
        self,
        targets: list[WatchTarget],
        products: dict[int, PriceSnapshot],
        errors: dict[int, str] | None = None,
    ) -> None:
        missing: list[int] = []
        errors = errors or {}
        for target in targets:
            snapshot = products.get(target.nm_id)
            if snapshot is None:
                message = errors.get(target.nm_id)
                if message:
                    await self._database.record_failures([target.product_id], message)
                else:
                    missing.append(target.product_id)
                continue
            await self._database.apply_snapshot(target.product_id, snapshot)
        if missing:
            await self._database.record_failures(
                missing, "Wildberries не вернул товар в пакетном ответе"
            )

    async def drain_outbox(self) -> None:
        for alert in await self._database.pending_alerts(limit=100):
            if alert.telegram_id not in self._settings.allowed_users:
                await self._database.discard_alert(
                    alert.outbox_id, "Telegram ID removed from allowlist"
                )
                continue
            await self._send_alert(alert)

    async def _send_alert(self, alert: PendingAlert) -> None:
        decision = alert.decision
        title = html.escape(alert.title)
        url = html.escape(alert.canonical_url, quote=True)
        source = source_label(alert.source)
        if decision.kind == "price_drop":
            percent = decision.drop_basis_points / 100
            text = (
                "🔻 <b>Цена снизилась</b>\n\n"
                f'<a href="{url}">{title}</a>\n'
                f"Было: <b>{format_money(decision.reference_price)}</b>\n"
                f"Стало: <b>{format_money(decision.current_price)}</b>\n"
                f"Снижение: {format_money(decision.drop_amount)} ({percent:g}%)\n"
                f"Источник: {source}"
            )
        elif decision.kind == "target":
            text = (
                "🎯 <b>Достигнута целевая цена</b>\n\n"
                f'<a href="{url}">{title}</a>\n'
                f"Текущая цена: <b>{format_money(decision.current_price)}</b>\n"
                f"Источник: {source}"
            )
        else:
            text = (
                "📦 <b>Товар снова в наличии</b>\n\n"
                f'<a href="{url}">{title}</a>\n'
                f"Цена: <b>{format_money(decision.current_price)}</b>\n"
                f"Источник: {source}"
            )
        now = asyncio.get_running_loop().time()
        previous = self._last_chat_send.get(alert.telegram_id)
        if previous is not None:
            await asyncio.sleep(max(0.0, 1.05 - (now - previous)))
        try:
            await self._bot.send_message(alert.telegram_id, text, disable_web_page_preview=False)
        except TelegramRetryAfter as exc:
            retry = max(1, int(exc.retry_after) + 1)
            await self._database.mark_alert_failed(alert.outbox_id, "Telegram 429", retry)
            logger.warning("Telegram ограничил чат %s на %s сек.", alert.telegram_id, retry)
        except TelegramAPIError as exc:
            retry = min(3600, 30 * (2 ** min(alert.attempts, 6)))
            await self._database.mark_alert_failed(alert.outbox_id, type(exc).__name__, retry)
            logger.warning("Не удалось отправить уведомление %s: %s", alert.telegram_id, exc)
        except Exception as exc:
            retry = min(3600, 30 * (2 ** min(alert.attempts, 6)))
            await self._database.mark_alert_failed(alert.outbox_id, type(exc).__name__, retry)
            logger.exception("Неожиданная ошибка отправки уведомления %s", alert.telegram_id)
        else:
            self._last_chat_send[alert.telegram_id] = asyncio.get_running_loop().time()
            await self._database.mark_alert_sent(alert.outbox_id)

    async def _send_account_error(self, telegram_id: int, error: str) -> None:
        text = (
            "⚠️ <b>Персональная проверка Wildberries приостановлена</b>\n\n"
            f"{html.escape(error)}\n\n"
            "Переподключите аккаунт через «👤 Аккаунт WB». "
            "На публичную цену бот автоматически не переключается."
        )
        try:
            await self._bot.send_message(telegram_id, text)
        except TelegramAPIError:
            logger.warning("Не удалось сообщить об истёкшей WB-сессии пользователю %s", telegram_id)

    async def _prune_if_needed(self) -> None:
        today = utcnow().date()
        if self._last_prune_date == today:
            return
        self._last_prune_date = today
        deleted = await self._database.prune_history(self._settings.price_history_days)
        if deleted:
            logger.info("Удалено старых записей истории: %s", deleted)


def source_label(source: str) -> str:
    if source.startswith("public_api:"):
        return "публичная цена"
    labels = {
        "public_api": "публичная цена",
        "account_browser": "цена аккаунта (beta)",
        "account_browser_wallet": "цена аккаунта с WB Кошельком (beta)",
        "context_reset": "ожидает новой контрольной цены",
    }
    return labels.get(source, source)


def seconds_since(value: datetime | None) -> int | None:
    normalized = normalize_datetime(value)
    if normalized is None:
        return None
    return max(0, int((utcnow() - normalized).total_seconds()))


def redacted_exception(exc: Exception) -> str:
    return type(exc).__name__


async def maybe_await(value: Any) -> Any:
    return await value if asyncio.iscoroutine(value) else value
