from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from wb_price_bot.config import Settings
from wb_price_bot.domain import PriceSnapshot


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_token="123456:abcdefghijklmnopqrstuvwxyzABCDE",
        session_encryption_key=Fernet.generate_key().decode("ascii"),
        allowed_users=frozenset({1001, 1002}),
        data_dir=tmp_path,
        check_interval_seconds=900,
        check_jitter_seconds=0,
        max_products_per_user=20,
        max_wb_batch_size=25,
        wb_destination=-5827722,
    )


def make_snapshot(
    *,
    nm_id: int = 28436956,
    price: int | None = 100_000,
    available: bool = True,
    source: str = "public_api",
    quantity: int = 5,
) -> PriceSnapshot:
    return PriceSnapshot(
        nm_id=nm_id,
        title="Тестовый товар",
        brand="Тест",
        price=price,
        basic_price=150_000,
        available=available,
        quantity=quantity,
        source=source,
        observed_at=datetime.now(UTC),
    )
