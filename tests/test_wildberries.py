from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from wb_price_bot.config import Settings
from wb_price_bot.domain import VariantSelection
from wb_price_bot.wildberries import (
    AccountProviderError,
    AccountWildberriesClient,
    MpstatsPriceClient,
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
                "supplier": "PalANtir",
                "supplierId": 134034,
                "totalQuantity": 7,
                "sizes": [
                    {
                        "name": "S",
                        "origName": "42",
                        "optionId": 1001,
                        "stocks": [{"qty": 0}],
                        "price": {"basic": 200_000, "product": 100_000, "logistics": 0},
                    },
                    {
                        "name": "M",
                        "origName": "44",
                        "optionId": 1002,
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
    assert first.size_name == "M"
    assert first.supplier_id == 134034
    assert len(result.variants[28436956]) == 2
    selected = result.select(28436956, VariantSelection(option_id=1001))
    assert selected is not None and selected.price == 100_000
    assert selected.available is False
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


@pytest.mark.asyncio
async def test_account_connector_uses_lightweight_authorized_request(
    settings: Settings,
) -> None:
    seen: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen = request
        return httpx.Response(200, json=_product_payload())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AccountWildberriesClient(settings, http)
    state = {
        "cookies": [
            {
                "name": "wb-card-session",
                "value": "card-cookie-secret",
                "domain": ".wb.ru",
                "path": "/",
            },
            {
                "name": "wb-id-only",
                "value": "must-not-leak",
                "domain": "id.wb.ru",
                "path": "/",
            },
            {
                "name": "storefront-only",
                "value": "must-not-leak-either",
                "domain": ".wildberries.ru",
                "path": "/",
            },
        ],
        "origins": [{"origin": "https://www.wildberries.ru", "localStorage": []}],
        "connector": {
            "version": 1,
            "cardUrl": "https://card.wb.ru/cards/v4/detail?appType=1&nm=1",
            "headers": {"authorization": "Bearer connector-secret-token"},
            "capturedAt": "2026-07-12T00:00:00Z",
        },
    }
    result = await client.fetch_many([28436956], json.dumps(state))
    assert result.products[28436956].price == 95_500
    assert result.products[28436956].source == "account_connector"
    assert seen is not None
    assert seen.url.params["nm"] == "28436956"
    assert seen.headers["authorization"] == "Bearer connector-secret-token"
    assert seen.headers["cookie"] == "wb-card-session=card-cookie-secret"
    await http.aclose()


@pytest.mark.asyncio
async def test_account_connector_does_not_launch_browser_on_transient_failure(
    settings: Settings,
) -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(498, text="challenge"))
    )
    client = AccountWildberriesClient(settings, http)
    state = {
        "cookies": [{"name": "wb-session", "value": "secret", "domain": ".wildberries.ru"}],
        "origins": [{"origin": "https://www.wildberries.ru", "localStorage": []}],
        "connector": {
            "version": 1,
            "cardUrl": "https://card.wb.ru/cards/v4/detail?appType=1&nm=1",
            "headers": {"authorization": "Bearer connector-secret-token"},
            "capturedAt": "2026-07-12T00:00:00Z",
        },
    }
    with pytest.raises(AccountProviderError, match="Wildberries временно ответил 498"):
        await client.fetch_many([28436956], json.dumps(state))
    await http.aclose()


@pytest.mark.asyncio
async def test_geo_location_resolves_destination(settings: Settings) -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "xinfo": "appType=1&curr=rub&dest=-5827722&spp=30",
                    "address": "ignored",
                },
            )
        )
    )
    client = PublicWildberriesClient(settings, http)
    region = await client.resolve_geo(52.286974, 104.305018)
    assert region.destination == -5827722
    assert region.label == "52.28697, 104.30502"
    await http.aclose()


@pytest.mark.asyncio
async def test_mpstats_licensed_provider_parses_price(settings: Settings) -> None:
    configured = replace(settings, mpstats_token="licensed-token")
    updated = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M:%S")
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "id": 28436956,
                    "name": "Товар",
                    "brand": "Brand",
                    "seller": {"id": 77, "name": "Seller"},
                    "stock": {"fbo": 4, "fbs": 1},
                    "price": {"price": 1000, "final_price": 675},
                    "updated": updated,
                },
                request=request,
            )
        )
    )
    client = MpstatsPriceClient(configured, http)
    result = await client.fetch_many([28436956])
    snapshot = result.products[28436956]
    assert snapshot.price == 67_500
    assert snapshot.basic_price == 100_000
    assert snapshot.quantity == 5
    assert snapshot.supplier_id == 77
    assert snapshot.source == "licensed_mpstats"
    await http.aclose()


@pytest.mark.asyncio
async def test_mpstats_rejects_stale_data(settings: Settings) -> None:
    configured = replace(settings, mpstats_token="licensed-token", mpstats_max_age_hours=24)
    stale = (datetime.now(ZoneInfo("Europe/Moscow")) - timedelta(days=3)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "name": "Товар",
                    "stock": {"fbo": 1},
                    "price": {"price": 1000, "final_price": 675},
                    "updated": stale,
                },
                request=request,
            )
        )
    )
    client = MpstatsPriceClient(configured, http)
    result = await client.fetch_many([28436956])
    assert result.products == {}
    assert "stale data" in result.errors[28436956]
    await http.aclose()
