from __future__ import annotations

import httpx
import pytest

from wb_price_bot.config import Settings
from wb_price_bot.wildberries import (
    ProductReferenceError,
    PublicWildberriesClient,
    WildberriesError,
    parse_price_text,
)


def _product_payload() -> dict[str, object]:
    return {
        "products": [
            {
                "id": 28436956,
                "name": "Краска по ткани",
                "brand": "Pebeo",
                "totalQuantity": 7,
                "sizes": [
                    {
                        "stocks": [{"qty": 0}],
                        "price": {"basic": 200_000, "product": 100_000, "logistics": 0},
                    },
                    {
                        "stocks": [{"qty": 7}],
                        "price": {"basic": 180_000, "product": 95_000, "logistics": 500},
                    },
                ],
            },
            {
                "id": 30379219,
                "name": "Нет в наличии",
                "brand": "Brand",
                "totalQuantity": 0,
                "sizes": [{"stocks": []}],
            },
        ]
    }


@pytest.mark.asyncio
async def test_public_client_parses_available_price_with_logistics(settings: Settings) -> None:
    seen_url: httpx.URL | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_url
        seen_url = request.url
        return httpx.Response(200, json=_product_payload())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = PublicWildberriesClient(settings, http)
    result = await client.fetch_many([28436956, 30379219])
    await http.aclose()

    first = result.products[28436956]
    assert first.price == 95_500
    assert first.basic_price == 180_000
    assert first.quantity == 7
    assert first.available is True
    second = result.products[30379219]
    assert second.price is None and second.available is False
    assert seen_url is not None
    assert seen_url.params["nm"] == "28436956;30379219"
    assert seen_url.params["dest"] == "-5827722"
    assert first.source == "public_api:-5827722"


@pytest.mark.asyncio
async def test_public_source_changes_with_destination(settings: Settings) -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_product_payload()))
    )
    client = PublicWildberriesClient(settings, http)
    result = await client.fetch_many([28436956], destination=-123456)
    assert result.products[28436956].source == "public_api:-123456"
    await http.aclose()


@pytest.mark.asyncio
async def test_unknown_schema_is_not_interpreted_as_zero(settings: Settings) -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"unexpected": []}))
    )
    client = PublicWildberriesClient(settings, http)
    with pytest.raises(WildberriesError, match="формат"):
        await client.fetch_many([28436956])
    await http.aclose()


@pytest.mark.asyncio
async def test_rate_limit_is_reported_without_retry_loop(settings: Settings) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = PublicWildberriesClient(settings, http)
    with pytest.raises(WildberriesError, match="429"):
        await client.fetch_many([28436956])
    assert calls == 1
    await http.aclose()


@pytest.mark.asyncio
async def test_short_link_redirect_stays_inside_allowlist(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "wb.ru"
        return httpx.Response(
            302,
            headers={"location": "https://www.wildberries.ru/catalog/28436956/detail.aspx"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = PublicWildberriesClient(settings, http)
    assert await client.resolve_reference("https://wb.ru/short") == 28436956
    await http.aclose()


@pytest.mark.asyncio
async def test_short_link_external_redirect_is_rejected(settings: Settings) -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"location": "https://example.com/steal"})
        )
    )
    client = PublicWildberriesClient(settings, http)
    with pytest.raises(ProductReferenceError, match="пределы"):
        await client.resolve_reference("https://wb.ru/short")
    await http.aclose()


def test_parse_price_text() -> None:
    assert parse_price_text("с WB Кошельком 1 199 ₽") == 119_900
    assert parse_price_text("нет цены") is None
