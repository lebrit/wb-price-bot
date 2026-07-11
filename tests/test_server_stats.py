from __future__ import annotations

from pathlib import Path

import pytest

from wb_price_bot.keyboards import main_keyboard
from wb_price_bot.server_stats import (
    _cpu_percent,
    collect_server_stats,
    format_bytes,
    format_duration,
)


def test_server_stat_formatters() -> None:
    assert format_bytes(1024**3) == "1.0 ГБ"
    assert format_duration(90_061) == "1 д. 1 ч. 1 мин."
    assert _cpu_percent((100, 40), (200, 60)) == 80.0


@pytest.mark.asyncio
async def test_collect_server_stats_has_disk_data(tmp_path: Path) -> None:
    stats = await collect_server_stats(tmp_path)
    assert stats.cpu_count >= 1
    assert stats.disk_total > 0
    assert 0 <= stats.disk_free <= stats.disk_total


def test_admin_button_is_only_added_for_admin() -> None:
    regular = [button.text for row in main_keyboard().keyboard for button in row]
    admin = [button.text for row in main_keyboard(is_admin=True).keyboard for button in row]
    assert "🛠 Админ-панель" not in regular
    assert "🛠 Админ-панель" in admin
