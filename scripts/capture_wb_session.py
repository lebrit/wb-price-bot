from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ручной вход в Wildberries и экспорт browser storage_state"
    )
    parser.add_argument("--output", default="wb-session.json")
    return parser.parse_args()


async def capture(output: Path) -> None:
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise SystemExit("Сначала установите помощник: pip install '.[browser]'") from exc

    print("Откроется отдельное окно браузера.")
    print("1. Войдите в свой аккаунт Wildberries обычным способом.")
    print("2. Выберите адрес доставки и откройте любую карточку товара.")
    print("3. Вернитесь в это окно терминала и нажмите Enter.")
    print("Телефон, SMS-код и CAPTCHA обрабатываются только сайтом Wildberries.\n")

    with tempfile.TemporaryDirectory(prefix="wb-price-bot-profile-") as profile_dir:
        async with async_playwright() as playwright:
            try:
                context = await playwright.chromium.launch_persistent_context(
                    profile_dir,
                    channel="chrome",
                    headless=False,
                    locale="ru-RU",
                    viewport={"width": 1365, "height": 900},
                )
            except PlaywrightError:
                try:
                    context = await playwright.chromium.launch_persistent_context(
                        profile_dir,
                        headless=False,
                        locale="ru-RU",
                        viewport={"width": 1365, "height": 900},
                    )
                except PlaywrightError as exc:
                    raise SystemExit(
                        "Chrome/Chromium не найден. Выполните: python -m playwright install chromium"
                    ) from exc
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.wildberries.ru/", wait_until="domcontentloaded")
            await asyncio.to_thread(input, "После входа нажмите Enter здесь… ")
            await page.goto("https://www.wildberries.ru/", wait_until="domcontentloaded")
            await page.evaluate("localStorage.setItem('_wb_price_bot_capture', '1')")
            try:
                state = await context.storage_state(indexed_db=True)
            except TypeError:
                state = await context.storage_state()
            await context.close()

    if not state.get("cookies"):
        raise SystemExit("Браузер не сохранил cookies Wildberries. Вход не подтверждён.")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        output.chmod(0o600)
    print(f"\nГотово: {output.resolve()}")
    print("Импортируйте файл прямо на свой сервер по SSH/stdin, затем удалите локальную копию.")


def main() -> None:
    args = parse_args()
    asyncio.run(capture(Path(args.output).expanduser().resolve()))


if __name__ == "__main__":
    main()
