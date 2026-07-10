from decimal import Decimal

import pytest

from wb_price_bot.domain import (
    ThresholdKind,
    calculate_drop_basis_points,
    evaluate_alert,
    extract_nm_id,
    format_money,
    parse_user_number,
    percent_to_basis_points,
    rubles_to_kopecks,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("28436956", 28436956),
        ("https://www.wildberries.ru/catalog/28436956/detail.aspx", 28436956),
        ("https://wildberries.ru/catalog/28436956/", 28436956),
        ("https://global.wildberries.ru/catalog/28436956/detail.aspx?size=42", 28436956),
        ("https://wb.ru/path?nm=28436956", 28436956),
    ],
)
def test_extract_nm_id(value: str, expected: int) -> None:
    assert extract_nm_id(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://wildberries.ru.evil.example/catalog/28436956/detail.aspx",
        "file:///catalog/28436956/detail.aspx",
        "https://127.0.0.1/catalog/28436956/detail.aspx",
        "1234",
        "not a link",
    ],
)
def test_extract_nm_id_rejects_unsafe_values(value: str) -> None:
    assert extract_nm_id(value) is None


def test_money_and_percent_conversion() -> None:
    assert parse_user_number(" 7,5 ") == Decimal("7.5")
    assert percent_to_basis_points(Decimal("7.5")) == 750
    assert rubles_to_kopecks(Decimal("2999.99")) == 299_999
    assert format_money(299_999) == "2 999,99 ₽"
    assert format_money(300_000) == "3 000 ₽"
    assert calculate_drop_basis_points(100_000, 90_000) == 1000


def test_percent_rule_accumulates_drop_and_resets_reference() -> None:
    decision, reference, latch = evaluate_alert(
        threshold_kind=ThresholdKind.PERCENT,
        threshold_value=1000,
        reference_price=100_000,
        previous_price=96_000,
        current_price=92_000,
        was_available=True,
        is_available=True,
        alert_latched=False,
    )
    assert decision is None
    assert reference == 100_000
    assert latch is False

    decision, reference, _ = evaluate_alert(
        threshold_kind=ThresholdKind.PERCENT,
        threshold_value=1000,
        reference_price=100_000,
        previous_price=92_000,
        current_price=89_000,
        was_available=True,
        is_available=True,
        alert_latched=False,
    )
    assert decision is not None
    assert decision.kind == "price_drop"
    assert decision.drop_amount == 11_000
    assert decision.drop_basis_points == 1100
    assert reference == 89_000


def test_amount_rule_uses_new_high_as_reference() -> None:
    decision, reference, _ = evaluate_alert(
        threshold_kind=ThresholdKind.AMOUNT,
        threshold_value=5000,
        reference_price=100_000,
        previous_price=100_000,
        current_price=105_000,
        was_available=True,
        is_available=True,
        alert_latched=False,
    )
    assert decision is None
    assert reference == 105_000


def test_target_rule_latches_until_price_rises() -> None:
    decision, _, latch = evaluate_alert(
        threshold_kind=ThresholdKind.TARGET,
        threshold_value=80_000,
        reference_price=100_000,
        previous_price=85_000,
        current_price=79_000,
        was_available=True,
        is_available=True,
        alert_latched=False,
    )
    assert decision is not None and decision.kind == "target"
    assert latch is True

    decision, _, latch = evaluate_alert(
        threshold_kind=ThresholdKind.TARGET,
        threshold_value=80_000,
        reference_price=100_000,
        previous_price=79_000,
        current_price=79_000,
        was_available=True,
        is_available=True,
        alert_latched=True,
    )
    assert decision is None and latch is True

    decision, _, latch = evaluate_alert(
        threshold_kind=ThresholdKind.TARGET,
        threshold_value=80_000,
        reference_price=100_000,
        previous_price=79_000,
        current_price=81_000,
        was_available=True,
        is_available=True,
        alert_latched=True,
    )
    assert decision is None and latch is False


def test_back_in_stock_is_separate_alert() -> None:
    decision, reference, latch = evaluate_alert(
        threshold_kind=ThresholdKind.AMOUNT,
        threshold_value=5000,
        reference_price=100_000,
        previous_price=100_000,
        current_price=99_000,
        was_available=False,
        is_available=True,
        alert_latched=False,
    )
    assert decision is not None and decision.kind == "back_in_stock"
    assert reference == 99_000
    assert latch is False


def test_back_in_stock_at_target_does_not_duplicate_target_alert() -> None:
    decision, reference, latch = evaluate_alert(
        threshold_kind=ThresholdKind.TARGET,
        threshold_value=80_000,
        reference_price=100_000,
        previous_price=100_000,
        current_price=79_000,
        was_available=False,
        is_available=True,
        alert_latched=False,
    )
    assert decision is not None and decision.kind == "back_in_stock"
    assert reference == 79_000
    assert latch is True

    decision, _, latch = evaluate_alert(
        threshold_kind=ThresholdKind.TARGET,
        threshold_value=80_000,
        reference_price=reference,
        previous_price=79_000,
        current_price=79_000,
        was_available=True,
        is_available=True,
        alert_latched=latch,
    )
    assert decision is None
    assert latch is True


def test_back_in_stock_alerts_even_when_initial_price_was_missing() -> None:
    decision, reference, _ = evaluate_alert(
        threshold_kind=ThresholdKind.PERCENT,
        threshold_value=1000,
        reference_price=None,
        previous_price=None,
        current_price=99_000,
        was_available=False,
        is_available=True,
        alert_latched=False,
    )
    assert decision is not None and decision.kind == "back_in_stock"
    assert reference == 99_000
