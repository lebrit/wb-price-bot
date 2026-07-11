from __future__ import annotations

import csv
import io
import json

from .domain import PriceRuleState
from .models import Product


def export_products(
    products: list[Product], rules: dict[int, list[PriceRuleState]], *, output_format: str
) -> bytes:
    rows = [_product_dict(product, rules.get(product.id, [])) for product in products]
    if output_format == "json":
        return json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
    if output_format != "csv":
        raise ValueError("Поддерживаются только CSV и JSON")
    output = io.StringIO(newline="")
    fields = list(rows[0]) if rows else ["nm_id", "url"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def _product_dict(product: Product, rules: list[PriceRuleState]) -> dict[str, object]:
    try:
        tags = json.loads(product.tags_json or "[]")
    except json.JSONDecodeError:
        tags = []
    return {
        "nm_id": product.nm_id,
        "url": product.canonical_url,
        "title": product.title,
        "brand": product.brand or "",
        "size": product.size_name or "",
        "option_id": product.option_id or "",
        "seller": product.supplier_name or "",
        "supplier_id": product.supplier_id or "",
        "folder": product.folder_name or "",
        "tags": ", ".join(str(item) for item in tags),
        "current_price_rub": product.current_price / 100 if product.current_price else "",
        "lowest_price_rub": product.lowest_price / 100 if product.lowest_price else "",
        "available": product.is_available,
        "active": product.is_active,
        "source": product.price_source,
        "rules": "; ".join(item.label() for item in rules),
        "last_checked_at": product.last_checked_at.isoformat() if product.last_checked_at else "",
    }
