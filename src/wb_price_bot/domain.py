from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from urllib.parse import urlparse

_PRODUCT_PATH_RE = re.compile(r"(?:^|/)catalog/(\d{5,15})(?:/|$)", re.IGNORECASE)
_ALLOWED_HOSTS = {
    "wildberries.ru",
    "www.wildberries.ru",
    "global.wildberries.ru",
    "wb.ru",
    "www.wb.ru",
}


class ThresholdKind(StrEnum):
    PERCENT = "percent"
    AMOUNT = "amount"
    TARGET = "target"


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    nm_id: int
    title: str
    brand: str | None
    price: int | None
    basic_price: int | None
    available: bool
    quantity: int
    source: str
    observed_at: datetime

    @property
    def url(self) -> str:
        return f"https://www.wildberries.ru/catalog/{self.nm_id}/detail.aspx"


@dataclass(frozen=True, slots=True)
class AlertDecision:
    kind: str
    reference_price: int | None
    current_price: int | None
    drop_amount: int = 0
    drop_basis_points: int = 0


def is_allowed_wb_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.rstrip(".").lower()
    return normalized in _ALLOWED_HOSTS


def extract_nm_id(value: str) -> int | None:
    candidate = value.strip()
    if candidate.isdecimal() and 5 <= len(candidate) <= 15:
        return int(candidate)
    if len(candidate) > 2048:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not is_allowed_wb_host(parsed.hostname):
        return None
    match = _PRODUCT_PATH_RE.search(parsed.path)
    if match:
        return int(match.group(1))
    for key in ("nm", "nm_id", "article"):
        query_match = re.search(rf"(?:^|[?&]){key}=(\d{{5,15}})(?:&|$)", candidate)
        if query_match:
            return int(query_match.group(1))
    return None


def parse_user_number(raw: str) -> Decimal:
    normalized = raw.strip().replace(" ", "").replace(",", ".")
    try:
        value = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("Введите положительное число") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("Введите положительное число")
    return value


def percent_to_basis_points(value: Decimal) -> int:
    if value > 100:
        raise ValueError("Процент не может быть больше 100")
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def rubles_to_kopecks(value: Decimal) -> int:
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_money(value: int | None) -> str:
    if value is None:
        return "—"
    rubles, kopecks = divmod(value, 100)
    grouped = f"{rubles:,}".replace(",", " ")
    return f"{grouped} ₽" if kopecks == 0 else f"{grouped},{kopecks:02d} ₽"


def calculate_drop_basis_points(reference: int, current: int) -> int:
    if reference <= 0 or current >= reference:
        return 0
    return ((reference - current) * 10_000) // reference


def evaluate_alert(
    *,
    threshold_kind: ThresholdKind,
    threshold_value: int,
    reference_price: int | None,
    previous_price: int | None,
    current_price: int | None,
    was_available: bool,
    is_available: bool,
    alert_latched: bool,
) -> tuple[AlertDecision | None, int | None, bool]:
    """Return alert, next reference price and latch state."""
    if is_available and not was_available:
        next_latch = (
            threshold_kind is ThresholdKind.TARGET
            and current_price is not None
            and current_price <= threshold_value
        )
        return (
            AlertDecision("back_in_stock", previous_price, current_price),
            current_price or reference_price,
            next_latch,
        )
    if current_price is None:
        return None, reference_price, alert_latched

    if threshold_kind is ThresholdKind.TARGET:
        if current_price <= threshold_value and not alert_latched:
            return (
                AlertDecision("target", previous_price, current_price),
                reference_price or current_price,
                True,
            )
        if current_price > threshold_value:
            alert_latched = False
        return None, reference_price or current_price, alert_latched

    if reference_price is None:
        return None, current_price, False
    if current_price >= reference_price:
        return None, current_price, False

    drop_amount = reference_price - current_price
    drop_bps = calculate_drop_basis_points(reference_price, current_price)
    threshold_reached = (
        drop_bps >= threshold_value
        if threshold_kind is ThresholdKind.PERCENT
        else drop_amount >= threshold_value
    )
    if threshold_reached:
        return (
            AlertDecision(
                "price_drop",
                reference_price,
                current_price,
                drop_amount=drop_amount,
                drop_basis_points=drop_bps,
            ),
            current_price,
            False,
        )
    return None, reference_price, False


def utcnow() -> datetime:
    return datetime.now(UTC)
