from __future__ import annotations

import json
from datetime import timedelta

import pytest

from wb_price_bot.charts import render_price_chart
from wb_price_bot.config import Settings
from wb_price_bot.database import Database
from wb_price_bot.domain import ThresholdKind, utcnow
from wb_price_bot.exports import export_products

from .conftest import make_snapshot


@pytest.mark.asyncio
async def test_chart_and_exports(settings: Settings) -> None:
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
    await database.apply_snapshot(product.id, make_snapshot(price=95_000))
    rows = await database.history_since(1001, product.id, since=utcnow() - timedelta(days=1))
    image = render_price_chart(rows, title=product.title, days=7)
    assert image.startswith(b"\x89PNG\r\n\x1a\n")

    products, _ = await database.list_products(1001, limit=100)
    rules = {product.id: await database.product_rules(1001, product.id)}
    json_bytes = export_products(products, rules, output_format="json")
    csv_bytes = export_products(products, rules, output_format="csv")
    assert json.loads(json_bytes)[0]["nm_id"] == product.nm_id
    assert csv_bytes.startswith(b"\xef\xbb\xbf")
    assert b"nm_id" in csv_bytes
    await database.close()
