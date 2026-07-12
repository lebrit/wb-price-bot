from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet

from . import __version__
from .auth_web import run_auth_server
from .bot import run_bot
from .config import ConfigurationError, Settings
from .database import Database, normalize_datetime
from .domain import format_money, utcnow
from .security import SessionCipher, SessionFormatError, normalize_wb_session
from .wildberries import MpstatsPriceClient, PublicWildberriesClient, WildberriesError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wb-price-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="Запустить Telegram-бота")
    subparsers.add_parser("auth-server", help="Запустить Telegram-форму входа WB")
    subparsers.add_parser("healthcheck", help="Проверить процесс и SQLite")
    subparsers.add_parser("integrity-check", help="Запустить PRAGMA integrity_check")
    subparsers.add_parser("stats", help="Показать статистику")
    subparsers.add_parser("generate-key", help="Создать ключ шифрования Fernet")
    subparsers.add_parser("version", help="Показать версию")

    backup = subparsers.add_parser("backup", help="Создать согласованную копию SQLite")
    backup.add_argument("destination", nargs="?", help="Путь к итоговому sqlite3-файлу")

    set_session = subparsers.add_parser(
        "set-session", help="Импортировать Playwright storage_state из stdin"
    )
    set_session.add_argument("--telegram-id", type=int, required=True)

    remove_session = subparsers.add_parser("remove-session", help="Удалить WB-сессию")
    remove_session.add_argument("--telegram-id", type=int, required=True)

    check_wb = subparsers.add_parser("check-wb", help="Проверить публичную карточку WB")
    check_wb.add_argument("reference", help="Ссылка или артикул")
    check_mpstats = subparsers.add_parser(
        "check-mpstats", help="Проверить лицензированный fallback MPSTATS"
    )
    check_mpstats.add_argument("nm_id", type=int, nargs="?", default=28436956)
    return parser


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.INFO)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    args = _parser().parse_args()
    if args.command == "generate-key":
        print(Fernet.generate_key().decode("ascii"))
        return
    if args.command == "version":
        print(__version__)
        return
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    configure_logging(settings.log_level)
    try:
        code = asyncio.run(_run_command(args, settings))
    except (RuntimeError, ValueError, WildberriesError, SessionFormatError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    raise SystemExit(code)


async def _run_command(args: argparse.Namespace, settings: Settings) -> int:
    if args.command == "run":
        await run_bot(settings)
        return 0
    if args.command == "auth-server":
        await run_auth_server(settings)
        return 0

    database = Database(settings.database_path)
    await database.initialize()
    try:
        if args.command == "healthcheck":
            await database.ping()
            if await database.integrity_check() != "ok":
                print("SQLite integrity_check failed", file=sys.stderr)
                return 1
            stats = await database.stats()
            cycle = normalize_datetime(stats.last_monitor_cycle)
            if cycle is None:
                print("monitor heartbeat is missing", file=sys.stderr)
                return 1
            max_age = settings.check_interval_seconds * 2 + 300
            if (utcnow() - cycle).total_seconds() > max_age:
                print("monitor heartbeat is stale", file=sys.stderr)
                return 1
            print("ok")
            return 0

        if args.command == "integrity-check":
            integrity_result = await database.integrity_check()
            print(integrity_result)
            return 0 if integrity_result == "ok" else 1

        if args.command == "stats":
            stats = await database.stats()
            print(json.dumps(asdict(stats), ensure_ascii=False, default=str, indent=2))
            return 0

        if args.command == "backup":
            if args.destination:
                destination = Path(args.destination)
            else:
                stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
                destination = settings.data_dir / "backups" / f"wb-price-bot-{stamp}.sqlite3"
            await database.backup_to(destination)
            print(destination)
            return 0

        if args.command == "set-session":
            if args.telegram_id not in settings.allowed_users:
                raise RuntimeError("Telegram ID отсутствует в TELEGRAM_ALLOWED_USERS")
            raw = sys.stdin.read()
            normalized = normalize_wb_session(raw)
            user = await database.ensure_user(
                args.telegram_id, None, f"Telegram {args.telegram_id}", is_admin=True
            )
            cipher = SessionCipher(settings.session_encryption_key)
            await database.save_wb_account(user.telegram_id, cipher.encrypt(normalized))
            print("WB-сессия импортирована")
            return 0

        if args.command == "remove-session":
            deleted = await database.delete_wb_account(args.telegram_id)
            print("WB-сессия удалена" if deleted else "WB-сессия не найдена")
            return 0

        if args.command == "check-wb":
            client = PublicWildberriesClient(settings)
            try:
                nm_id = await client.resolve_reference(args.reference)
                fetch_result = await client.fetch_many([nm_id])
            finally:
                await client.close()
            snapshot = fetch_result.products.get(nm_id)
            if snapshot is None:
                raise RuntimeError("Товар не найден")
            print(
                json.dumps(
                    {
                        "nm_id": snapshot.nm_id,
                        "title": snapshot.title,
                        "price": format_money(snapshot.price),
                        "available": snapshot.available,
                        "quantity": snapshot.quantity,
                        "source": snapshot.source,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.command == "check-mpstats":
            mpstats_client = MpstatsPriceClient(settings)
            try:
                result = await mpstats_client.fetch_many([args.nm_id])
            finally:
                await mpstats_client.close()
            snapshot = result.products.get(args.nm_id)
            if snapshot is None:
                raise RuntimeError(result.errors.get(args.nm_id, "MPSTATS не вернул товар"))
            print(
                json.dumps(
                    {
                        "nm_id": snapshot.nm_id,
                        "title": snapshot.title,
                        "price": format_money(snapshot.price),
                        "available": snapshot.available,
                        "source": snapshot.source,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
    finally:
        await database.close()
    return 2


if __name__ == "__main__":
    main()
