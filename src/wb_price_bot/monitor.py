from __future__ import annotations

import asyncio
import html
import logging
import random
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from .config import Settings
from .database import Database, PendingAlert, WatchTarget, normalize_datetime
from .domain import PriceSnapshot, VariantSelection, format_money, is_quiet_time, utcnow
from .security import SessionCipher
from .wildberries import (
    AccountProviderError,
    AccountSessionError,
    AccountWildberriesClient,
    FetchResult,
    LicensedProviderError,
    MpstatsPriceClient,
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
        licensed_client: MpstatsPriceClient | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._bot = bot
        self._public = public_client
        self._account = account_client
        self._licensed = licensed_client
        self._cipher = cipher
        self._stop = asyncio.Event()
        self._cycle_lock = asyncio.Lock()
        self._last_prune_date: date | None = None
        self._last_chat_send: dict[int, float] = {}
        self._timezone = ZoneInfo(settings.timezone_name)

    async def run(self) -> None:
        logger.info("Мониторинг цен запущен")
        while not self._stop.is_set():
            started = utcnow()
            try:
                await self.check_all()
                await self.send_due_digests()
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
            allowed_users = await self._allowed_users()
            targets = await self._database.active_targets(
                allowed_users, self._settings.wb_destination
            )
            public_targets = [item for item in targets if item.account_status is None]
            public_groups: dict[int, list[WatchTarget]] = defaultdict(list)
            for target in public_targets:
                public_groups[target.destination].append(target)
            for destination, group in public_groups.items():
                await self._check_public(group, destination)

            account_groups: dict[int, list[WatchTarget]] = defaultdict(list)
            for target in targets:
                if target.account_status is not None:
                    account_groups[target.user_id].append(target)
            for group in account_groups.values():
                await self._check_account(group)
            await self.drain_outbox(allowed_users)

    async def check_product(self, telegram_id: int, product_id: int) -> PriceSnapshot:
        async with self._cycle_lock:
            product = await self._database.get_product(telegram_id, product_id)
            if product is None:
                raise WildberriesError("Товар не найден")
            selection = VariantSelection(
                option_id=product.option_id,
                size_name=product.size_name,
                supplier_id=product.supplier_id,
                supplier_name=product.supplier_name,
            )
            account = await self._database.get_wb_account(telegram_id)
            if account is not None:
                if account.status != "active":
                    raise AccountSessionError(
                        "WB-сессия требует обновления; публичная цена не подставлена"
                    )
                try:
                    state = self._cipher.decrypt(account.encrypted_session)
                    result = await self._account.fetch_many(
                        [product.nm_id], state, {product.nm_id: selection}
                    )
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
                user = await self._database.get_user(telegram_id)
                destination = (
                    user.wb_destination
                    if user is not None and user.wb_destination is not None
                    else self._settings.wb_destination
                )
                try:
                    result = await self._public.fetch_many([product.nm_id], destination=destination)
                except WildberriesError:
                    if (
                        selection.option_id is not None
                        or self._licensed is None
                        or not self._licensed.enabled
                    ):
                        raise
                    result = await self._licensed.fetch_many([product.nm_id])
            snapshot = result.select(product.nm_id, selection)
            if snapshot is None:
                product_error = result.errors.get(product.nm_id)
                if product_error:
                    raise WildberriesError(product_error)
                raise WildberriesError("Wildberries не вернул данные этого товара")
            await self._database.apply_snapshot(product.id, snapshot)
            await self.drain_outbox()
            return snapshot

    async def _check_public(self, targets: list[WatchTarget], destination: int) -> None:
        ids = list(dict.fromkeys(item.nm_id for item in targets))
        try:
            result = await self._public.fetch_many(ids, destination=destination)
        except WildberriesError as exc:
            logger.warning("Публичная проверка WB не выполнена: %s", exc)
            eligible = [item for item in targets if item.option_id is None]
            ineligible = [item for item in targets if item.option_id is not None]
            if not eligible or self._licensed is None or not self._licensed.enabled:
                await self._database.record_failures(
                    [item.product_id for item in targets], str(exc)
                )
                return
            if ineligible:
                await self._database.record_failures(
                    [item.product_id for item in ineligible],
                    "Лицензированный fallback не подменяет цену выбранного размера",
                )
            try:
                assert self._licensed is not None
                result = await self._licensed.fetch_many(
                    list(dict.fromkeys(item.nm_id for item in eligible))
                )
            except LicensedProviderError as licensed_exc:
                await self._database.record_failures(
                    [item.product_id for item in eligible], str(licensed_exc)
                )
                return
            await self._apply_fetch_result(eligible, result)
            return
        await self._apply_fetch_result(targets, result)

    async def _apply_fetch_result(self, targets: list[WatchTarget], result: FetchResult) -> None:
        for target in targets:
            selection = VariantSelection(
                option_id=target.option_id,
                size_name=target.size_name,
                supplier_id=target.supplier_id,
                supplier_name=target.supplier_name,
            )
            snapshot = result.select(target.nm_id, selection)
            if snapshot is None:
                await self._database.record_failures(
                    [target.product_id],
                    result.errors.get(
                        target.nm_id, "Выбранный размер или продавец больше не доступен"
                    ),
                )
                continue
            if (
                target.supplier_id is not None
                and snapshot.supplier_id is not None
                and snapshot.supplier_id != target.supplier_id
            ):
                await self._database.record_failures(
                    [target.product_id], "Выбранный продавец изменился"
                )
                continue
            await self._database.apply_snapshot(target.product_id, snapshot)

    async def _check_account(self, targets: list[WatchTarget]) -> None:
        first = targets[0]
        if first.account_status != "active":
            return
        assert first.encrypted_session is not None
        try:
            state = self._cipher.decrypt(first.encrypted_session)
            result = await self._account.fetch_many(
                list(dict.fromkeys(item.nm_id for item in targets)),
                state,
                {
                    item.nm_id: VariantSelection(
                        option_id=item.option_id,
                        size_name=item.size_name,
                        supplier_id=item.supplier_id,
                        supplier_name=item.supplier_name,
                    )
                    for item in targets
                },
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

    async def drain_outbox(self, allowed_users: set[int] | None = None) -> None:
        if allowed_users is None:
            allowed_users = await self._allowed_users()
        preferences = await self._database.notification_preferences(allowed_users)
        local_time = utcnow().astimezone(self._timezone).time()
        for alert in await self._database.pending_alerts(limit=100):
            if alert.telegram_id not in allowed_users:
                await self._database.discard_alert(
                    alert.outbox_id, "Telegram ID removed from allowlist"
                )
                continue
            preference = preferences.get(alert.telegram_id)
            if preference and is_quiet_time(
                preference.quiet_start_minute,
                preference.quiet_end_minute,
                local_time,
            ):
                continue
            await self._send_alert(alert)

    async def send_due_digests(self) -> None:
        now = utcnow().astimezone(self._timezone)
        minute = now.hour * 60 + now.minute
        allowed_users = await self._allowed_users()
        preferences = await self._database.notification_preferences(allowed_users)
        for preference in preferences.values():
            if (
                not preference.daily_digest_enabled
                or preference.last_digest_date == now.date().isoformat()
                or minute < preference.daily_digest_minute
                or is_quiet_time(
                    preference.quiet_start_minute,
                    preference.quiet_end_minute,
                    now.time(),
                )
            ):
                continue
            items = await self._database.digest_items(
                preference.telegram_id, since=utcnow() - timedelta(days=1)
            )
            available = sum(item.is_available for item in items)
            drops = [
                item
                for item in items
                if item.current_price is not None
                and item.first_price is not None
                and item.current_price < item.first_price
            ]
            lines = [
                "📋 <b>Дневная сводка WB Price Bot</b>",
                "",
                f"Товаров: {len(items)}; в наличии: {available}; подешевели за сутки: {len(drops)}.",
            ]
            for item in sorted(
                drops,
                key=lambda value: cast(int, value.first_price) - cast(int, value.current_price),
                reverse=True,
            )[:15]:
                lines.append(
                    f'• <a href="{html.escape(item.canonical_url, quote=True)}">'
                    f"{html.escape(item.title[:70])}</a>: "
                    f"{format_money(item.first_price)} → {format_money(item.current_price)}"
                )
            try:
                await self._bot.send_message(
                    preference.telegram_id,
                    "\n".join(lines),
                    disable_web_page_preview=True,
                )
            except TelegramAPIError as exc:
                logger.warning(
                    "Не удалось отправить дневную сводку %s: %s",
                    preference.telegram_id,
                    exc,
                )
                continue
            await self._database.mark_digest_sent(preference.telegram_id, now.date())

    async def _allowed_users(self) -> set[int]:
        if self._settings.registration_mode == "allowlist":
            return set(self._settings.allowed_users)
        return await self._database.approved_telegram_ids(self._settings.allowed_users)

    async def _send_alert(self, alert: PendingAlert) -> None:
        decision = alert.decision
        title = html.escape(alert.title)
        url = html.escape(alert.canonical_url, quote=True)
        source = source_label(alert.source)
        rule = _rule_label(alert.rule_kind, alert.rule_value)
        if decision.kind == "price_drop":
            percent = decision.drop_basis_points / 100
            text = (
                "🔻 <b>Цена снизилась</b>\n\n"
                f'<a href="{url}">{title}</a>\n'
                f"Было: <b>{format_money(decision.reference_price)}</b>\n"
                f"Стало: <b>{format_money(decision.current_price)}</b>\n"
                f"Снижение: {format_money(decision.drop_amount)} ({percent:g}%)\n"
                f"Правило: {rule}\n"
                f"Источник: {source}"
            )
        elif decision.kind == "target":
            text = (
                "🎯 <b>Достигнута целевая цена</b>\n\n"
                f'<a href="{url}">{title}</a>\n'
                f"Текущая цена: <b>{format_money(decision.current_price)}</b>\n"
                f"Правило: {rule}\n"
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
        await self._database.expire_auth_sessions()
        await self._database.prune_auth_sessions()
        if deleted:
            logger.info("Удалено старых записей истории: %s", deleted)


def source_label(source: str) -> str:
    if source.startswith("public_api:"):
        return "публичная цена"
    labels = {
        "public_api": "публичная цена",
        "account_browser": "цена аккаунта (beta)",
        "account_browser_wallet": "цена аккаунта с WB Кошельком (beta)",
        "account_connector": "персональная цена аккаунта WB (beta)",
        "context_reset": "ожидает новой контрольной цены",
        "licensed_mpstats": "лицензированный fallback MPSTATS",
    }
    return labels.get(source, source)


def _rule_label(kind: Any, value: int | None) -> str:
    if kind is None or value is None:
        return "старое правило"
    if str(kind) == "percent":
        return f"снижение на {value / 100:g}%"
    if str(kind) == "amount":
        return f"снижение на {format_money(value)}"
    return f"цена не выше {format_money(value)}"


def seconds_since(value: datetime | None) -> int | None:
    normalized = normalize_datetime(value)
    if normalized is None:
        return None
    return max(0, int((utcnow() - normalized).total_seconds()))


def redacted_exception(exc: Exception) -> str:
    return type(exc).__name__


async def maybe_await(value: Any) -> Any:
    return await value if asyncio.iscoroutine(value) else value
