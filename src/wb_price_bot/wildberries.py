from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import shutil
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx

from .config import Settings
from .domain import (
    GeoRegion,
    PriceSnapshot,
    VariantSelection,
    extract_nm_id,
    is_allowed_wb_host,
    utcnow,
)

logger = logging.getLogger(__name__)

_CARD_PATH_FRAGMENT = "/cards/v4/detail"
_PRICE_TEXT_RE = re.compile(r"(\d[\d\s\u00a0]{0,15})\s*[₽р]", re.IGNORECASE)
_PUBLIC_USER_AGENT = "WB-Price-Bot/0.3 (+https://github.com/lebrit/wb-price-bot)"


class WildberriesError(RuntimeError):
    pass


class ProductReferenceError(WildberriesError):
    pass


class AccountSessionError(WildberriesError):
    pass


class AccountProviderError(WildberriesError):
    pass


class AccountProductError(WildberriesError):
    pass


class LicensedProviderError(WildberriesError):
    pass


@dataclass(frozen=True, slots=True)
class ProductVariant:
    nm_id: int
    title: str
    brand: str | None
    option_id: int
    size_name: str
    original_size_name: str
    price: int | None
    basic_price: int | None
    available: bool
    quantity: int
    supplier_id: int | None
    supplier_name: str | None

    def snapshot(self, source: str) -> PriceSnapshot:
        return PriceSnapshot(
            nm_id=self.nm_id,
            title=self.title,
            brand=self.brand,
            price=self.price,
            basic_price=self.basic_price,
            available=self.available,
            quantity=self.quantity,
            source=source,
            observed_at=utcnow(),
            option_id=self.option_id,
            size_name=self.size_name,
            supplier_id=self.supplier_id,
            supplier_name=self.supplier_name,
        )


@dataclass(frozen=True, slots=True)
class FetchResult:
    products: dict[int, PriceSnapshot]
    refreshed_session: str | None = None
    errors: dict[int, str] = field(default_factory=dict)
    variants: dict[int, list[ProductVariant]] = field(default_factory=dict)

    def select(self, nm_id: int, selection: VariantSelection | None = None) -> PriceSnapshot | None:
        selection = selection or VariantSelection()
        if selection.supplier_id is not None:
            candidates = self.variants.get(nm_id, [])
            if candidates and all(item.supplier_id != selection.supplier_id for item in candidates):
                return None
        if selection.option_id is not None:
            for variant in self.variants.get(nm_id, []):
                if variant.option_id == selection.option_id and (
                    selection.supplier_id is None or variant.supplier_id == selection.supplier_id
                ):
                    source = self.products.get(nm_id)
                    return variant.snapshot(source.source if source else "public_api")
            return None
        return self.products.get(nm_id)


def _chunks(values: list[int], size: int) -> Iterable[list[int]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _parse_price(value: Any) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def _parse_product(
    product: dict[str, Any], source: str
) -> tuple[PriceSnapshot, list[ProductVariant]] | None:
    nm_id = product.get("id")
    if not isinstance(nm_id, int) or nm_id <= 0:
        return None
    sizes = product.get("sizes")
    if not isinstance(sizes, list):
        sizes = []

    name = str(product.get("name") or f"Товар {nm_id}").strip()
    brand_value = product.get("brand")
    brand = str(brand_value).strip() if brand_value else None
    supplier_id = product.get("supplierId")
    supplier_id = supplier_id if isinstance(supplier_id, int) and supplier_id > 0 else None
    supplier_value = product.get("supplier")
    supplier_name = str(supplier_value).strip() if supplier_value else None

    variants: list[ProductVariant] = []
    fallback_prices: list[tuple[int, int | None, bool]] = []
    quantity = 0
    for size in sizes:
        if not isinstance(size, dict):
            continue
        size_quantity = 0
        stocks = size.get("stocks")
        if isinstance(stocks, list):
            for stock in stocks:
                if isinstance(stock, dict) and isinstance(stock.get("qty"), int):
                    size_quantity += max(0, stock["qty"])
        quantity += size_quantity
        price_data = size.get("price")
        if not isinstance(price_data, dict):
            continue
        product_price = _parse_price(price_data.get("product"))
        logistics = price_data.get("logistics")
        if product_price is not None and isinstance(logistics, int) and logistics > 0:
            product_price += logistics
        if product_price is not None:
            fallback_prices.append(
                (
                    product_price,
                    _parse_price(price_data.get("basic")),
                    size_quantity > 0,
                )
            )
        option_id = size.get("optionId")
        if not isinstance(option_id, int) or option_id <= 0:
            continue
        size_name = str(size.get("name") or size.get("origName") or "Без размера").strip()
        original_size_name = str(size.get("origName") or size_name).strip()
        variants.append(
            ProductVariant(
                nm_id=nm_id,
                title=name,
                brand=brand,
                option_id=option_id,
                size_name=size_name or "Без размера",
                original_size_name=original_size_name or size_name or "Без размера",
                price=product_price,
                basic_price=_parse_price(price_data.get("basic")),
                available=size_quantity > 0,
                quantity=size_quantity,
                supplier_id=supplier_id,
                supplier_name=supplier_name,
            )
        )

    available_variants = [item for item in variants if item.available]
    candidates = available_variants or variants
    priced = [item for item in candidates if item.price is not None]
    selected = min(priced, key=lambda item: cast(int, item.price)) if priced else None
    available_fallback = [item for item in fallback_prices if item[2]]
    fallback_candidates = available_fallback or fallback_prices
    fallback_selected = min(fallback_candidates, key=lambda item: item[0], default=None)
    total_quantity = product.get("totalQuantity")
    if isinstance(total_quantity, int):
        quantity = max(quantity, total_quantity)
    snapshot = PriceSnapshot(
        nm_id=nm_id,
        title=name,
        brand=brand,
        price=selected.price if selected else (fallback_selected[0] if fallback_selected else None),
        basic_price=(
            selected.basic_price
            if selected
            else (fallback_selected[1] if fallback_selected else None)
        ),
        available=quantity > 0,
        quantity=quantity,
        source=source,
        observed_at=utcnow(),
        option_id=selected.option_id if selected else None,
        size_name=selected.size_name if selected else None,
        supplier_id=supplier_id,
        supplier_name=supplier_name,
    )
    return snapshot, variants


def _parse_products(
    payload: Any, source: str
) -> tuple[dict[int, PriceSnapshot], dict[int, list[ProductVariant]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
        raise WildberriesError("Wildberries вернул неизвестный формат данных")
    parsed: dict[int, PriceSnapshot] = {}
    variants: dict[int, list[ProductVariant]] = {}
    for item in payload["products"]:
        if not isinstance(item, dict):
            continue
        result = _parse_product(item, source)
        if result is not None:
            snapshot, product_variants = result
            parsed[snapshot.nm_id] = snapshot
            variants[snapshot.nm_id] = product_variants
    return parsed, variants


class PublicWildberriesClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={
                "User-Agent": _PUBLIC_USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def resolve_reference(self, value: str) -> int:
        direct = extract_nm_id(value)
        if direct is not None:
            return direct
        candidate = value.strip()
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not is_allowed_wb_host(parsed.hostname):
            raise ProductReferenceError("Отправьте ссылку Wildberries или числовой артикул")

        current = candidate
        for _ in range(5):
            response = await self._http.get(current, follow_redirects=False)
            if response.status_code not in {301, 302, 303, 307, 308}:
                resolved = extract_nm_id(str(response.url))
                if resolved is not None:
                    return resolved
                break
            location = response.headers.get("location")
            if not location:
                break
            current = urljoin(current, location)
            redirect = urlparse(current)
            if redirect.scheme not in {"http", "https"} or not is_allowed_wb_host(
                redirect.hostname
            ):
                raise ProductReferenceError("Короткая ссылка ведёт за пределы Wildberries")
            resolved = extract_nm_id(current)
            if resolved is not None:
                return resolved
        raise ProductReferenceError("Не удалось найти артикул в ссылке")

    async def resolve_geo(self, latitude: float, longitude: float) -> GeoRegion:
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise WildberriesError("Telegram передал некорректные координаты")
        try:
            response = await self._http.get(
                self._settings.wb_geo_url,
                params={
                    "latitude": f"{latitude:.6f}",
                    "longitude": f"{longitude:.6f}",
                    "address": f"Telegram {latitude:.5f},{longitude:.5f}",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WildberriesError("Wildberries не определил регион по геопозиции") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("xinfo"), str):
            raise WildberriesError("Wildberries вернул неизвестный формат региона")
        xinfo = parse_qs(payload["xinfo"])
        try:
            destination = int(xinfo["dest"][0])
        except (KeyError, IndexError, ValueError) as exc:
            raise WildberriesError("Wildberries не вернул dest для геопозиции") from exc
        label = f"{latitude:.5f}, {longitude:.5f}"
        return GeoRegion(destination, latitude, longitude, label[:200])

    async def fetch_many(self, nm_ids: list[int], *, destination: int | None = None) -> FetchResult:
        unique_ids = list(dict.fromkeys(nm_ids))
        result: dict[int, PriceSnapshot] = {}
        variants: dict[int, list[ProductVariant]] = {}
        for chunk in _chunks(unique_ids, self._settings.max_wb_batch_size):
            params = self._params(chunk, destination)
            try:
                response = await self._http.get(self._settings.wb_api_url, params=params)
            except httpx.HTTPError as exc:
                raise WildberriesError(f"Ошибка сети Wildberries: {type(exc).__name__}") from exc
            if response.status_code == 429:
                raise WildberriesError("Wildberries ограничил частоту запросов (429)")
            if response.status_code in {403, 498}:
                if not self._owns_client:
                    raise WildberriesError(
                        f"Wildberries отклонил автоматический запрос ({response.status_code})"
                    )
                logger.info(
                    "WB вернул %s встроенному HTTP-клиенту; повторяю через системный curl",
                    response.status_code,
                )
                curl_products, curl_variants = await self._fetch_with_curl(chunk, destination)
                result.update(curl_products)
                variants.update(curl_variants)
                continue
            try:
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise WildberriesError(
                    f"Wildberries вернул ошибочный ответ ({response.status_code})"
                ) from exc
            source = f"public_api:{destination or self._settings.wb_destination}"
            parsed, parsed_variants = _parse_products(payload, source)
            result.update(parsed)
            variants.update(parsed_variants)
        return FetchResult(result, variants=variants)

    def _params(self, nm_ids: list[int], destination: int | None) -> dict[str, str | int]:
        return {
            "appType": 1,
            "curr": self._settings.wb_currency,
            "dest": destination or self._settings.wb_destination,
            "spp": 30,
            "hide_vflags": 4294967296,
            "hide_dtype": 15,
            "mtype": 257,
            "lang": self._settings.wb_language,
            "ab_testing": "false",
            "nm": ";".join(str(item) for item in nm_ids),
        }

    async def _fetch_with_curl(
        self, nm_ids: list[int], destination: int | None
    ) -> tuple[dict[int, PriceSnapshot], dict[int, list[ProductVariant]]]:
        curl = shutil.which("curl")
        if curl is None:
            raise WildberriesError("WB требует резервный запрос, но системный curl не найден")
        url = str(httpx.URL(self._settings.wb_api_url, params=self._params(nm_ids, destination)))
        process = await asyncio.create_subprocess_exec(
            curl,
            "--silent",
            "--show-error",
            "--compressed",
            "--max-time",
            "25",
            "--proto",
            "=https",
            "--header",
            "Accept: application/json, text/plain, */*",
            "--header",
            "Accept-Language: ru-RU,ru;q=0.9",
            "--user-agent",
            _PUBLIC_USER_AGENT,
            "--write-out",
            "\n%{http_code}",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise WildberriesError("Резервный запрос curl превысил тайм-аут") from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[:200]
            raise WildberriesError(
                f"curl не получил ответ Wildberries: {detail or 'network error'}"
            )
        try:
            body, raw_status = stdout.rsplit(b"\n", 1)
            status = int(raw_status)
        except (ValueError, TypeError) as exc:
            raise WildberriesError("curl вернул ответ без HTTP-статуса") from exc
        if status == 429:
            raise WildberriesError("Wildberries ограничил частоту резервного запроса (429)")
        if status in {403, 498}:
            raise WildberriesError(f"Wildberries отклонил резервный запрос ({status})")
        if status < 200 or status >= 300:
            raise WildberriesError(f"Wildberries вернул резервному запросу HTTP {status}")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise WildberriesError("curl получил от Wildberries не JSON") from exc
        source = f"public_api:{destination or self._settings.wb_destination}"
        return _parse_products(payload, source)


class MpstatsPriceClient:
    """Optional licensed fallback. It is never used for account or size-specific prices."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=httpx.Timeout(20.0))

    @property
    def enabled(self) -> bool:
        return bool(self._settings.mpstats_token)

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def fetch_many(self, nm_ids: list[int]) -> FetchResult:
        if not self.enabled:
            raise LicensedProviderError("MPSTATS token не настроен")
        products: dict[int, PriceSnapshot] = {}
        errors: dict[int, str] = {}
        for nm_id in dict.fromkeys(nm_ids):
            try:
                response = await self._http.get(
                    f"{self._settings.mpstats_api_url.rstrip('/')}/items/{nm_id}/full",
                    headers={"X-Mpstats-TOKEN": self._settings.mpstats_token},
                )
            except httpx.HTTPError as exc:
                errors[nm_id] = f"MPSTATS network error: {type(exc).__name__}"
                continue
            if response.status_code in {401, 403}:
                raise LicensedProviderError("MPSTATS отклонил API token или тариф не даёт доступ")
            if response.status_code == 429:
                raise LicensedProviderError("MPSTATS исчерпал лимит запросов")
            if response.status_code != 200:
                errors[nm_id] = f"MPSTATS HTTP {response.status_code}"
                continue
            try:
                payload = response.json()
                snapshot = self._parse(nm_id, payload)
            except (ValueError, TypeError, KeyError) as exc:
                errors[nm_id] = f"MPSTATS data error: {exc or type(exc).__name__}"
                continue
            products[nm_id] = snapshot
        return FetchResult(products, errors=errors)

    def _parse(self, nm_id: int, payload: Any) -> PriceSnapshot:
        if not isinstance(payload, dict):
            raise ValueError("payload")
        price_data = payload.get("price")
        if not isinstance(price_data, dict):
            raise ValueError("price")
        final_rubles = price_data.get("final_price")
        basic_rubles = price_data.get("price")
        if not isinstance(final_rubles, int | float) or final_rubles <= 0:
            raise ValueError("final_price")
        stock_data = payload.get("stock")
        quantity = 0
        if isinstance(stock_data, dict):
            quantity = sum(
                max(0, int(value))
                for value in stock_data.values()
                if isinstance(value, int | float)
            )
        seller = payload.get("seller")
        supplier_id = seller.get("id") if isinstance(seller, dict) else None
        supplier_name = seller.get("name") if isinstance(seller, dict) else None
        brand_value = payload.get("brand")
        if isinstance(brand_value, dict):
            brand_value = brand_value.get("name")
        updated = payload.get("updated")
        if not isinstance(updated, str):
            raise ValueError("updated")
        try:
            parsed = datetime.strptime(updated, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=ZoneInfo("Europe/Moscow")
            )
            observed_at = parsed.astimezone(UTC)
        except ValueError as exc:
            raise ValueError("updated") from exc
        if utcnow() - observed_at > timedelta(hours=self._settings.mpstats_max_age_hours):
            raise ValueError("stale data")
        return PriceSnapshot(
            nm_id=nm_id,
            title=str(payload.get("name") or f"Товар {nm_id}"),
            brand=str(brand_value or "") or None,
            price=round(float(final_rubles) * 100),
            basic_price=(
                round(float(basic_rubles) * 100)
                if isinstance(basic_rubles, int | float) and basic_rubles > 0
                else None
            ),
            available=quantity > 0,
            quantity=quantity,
            source="licensed_mpstats",
            observed_at=observed_at,
            supplier_id=int(supplier_id) if isinstance(supplier_id, int | float) else None,
            supplier_name=str(supplier_name) if supplier_name else None,
        )


class AccountWildberriesClient:
    """Experimental account-price adapter backed by a user-authorized browser state."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()

    async def fetch_many(
        self,
        nm_ids: list[int],
        session_state: str,
        selections: dict[int, VariantSelection] | None = None,
    ) -> FetchResult:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise AccountProviderError(
                "В образе не установлен браузерный модуль Playwright"
            ) from exc
        try:
            storage_state = json.loads(session_state)
        except json.JSONDecodeError as exc:
            raise AccountSessionError("Сохранённая WB-сессия повреждена") from exc
        if (
            not isinstance(storage_state, dict)
            or not isinstance(storage_state.get("cookies"), list)
            or not isinstance(storage_state.get("origins"), list)
        ):
            raise AccountSessionError("Сохранённая WB-сессия имеет неверный формат")

        unique_ids = list(dict.fromkeys(nm_ids))
        if not unique_ids:
            return FetchResult({})
        async with self._lock, async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(
                    headless=self._settings.wb_browser_headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
            except Exception as exc:
                raise AccountProviderError("Не удалось запустить Chromium для WB") from exc
            try:
                try:
                    context = await browser.new_context(
                        storage_state=cast(Any, storage_state),
                        locale="ru-RU",
                        viewport={"width": 1365, "height": 900},
                    )
                except Exception as exc:
                    raise AccountProviderError(
                        "Chromium не смог создать контекст для проверки WB"
                    ) from exc
                try:
                    page = await context.new_page()
                except Exception as exc:
                    raise AccountProviderError(
                        "Chromium не смог открыть страницу для проверки WB"
                    ) from exc
                products: dict[int, PriceSnapshot] = {}
                errors: dict[int, str] = {}
                for index, nm_id in enumerate(unique_ids):
                    try:
                        snapshot, _ = await self._fetch_product_page(
                            page, nm_id, (selections or {}).get(nm_id)
                        )
                    except AccountSessionError:
                        raise
                    except AccountProductError as exc:
                        errors[nm_id] = str(exc)
                        continue
                    if snapshot is not None:
                        products[nm_id] = snapshot
                    if index + 1 < len(unique_ids):
                        await asyncio.sleep(random.uniform(0.7, 1.5))
                serialized: str | None = None
                try:
                    try:
                        refreshed = await context.storage_state(indexed_db=True)
                    except TypeError:
                        refreshed = await context.storage_state()
                    serialized = json.dumps(
                        refreshed, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                    )
                except Exception as exc:
                    logger.warning(
                        "Не удалось обновить browser storage state WB: %s", type(exc).__name__
                    )
                return FetchResult(products, serialized, errors)
            finally:
                with suppress(Exception):
                    await browser.close()

    async def _fetch_product_page(
        self, page: Any, nm_id: int, selection: VariantSelection | None = None
    ) -> tuple[PriceSnapshot | None, bool]:
        response_queue: asyncio.Queue[Any] = asyncio.Queue()
        auth_seen = False
        snapshot: PriceSnapshot | None = None
        variants: list[ProductVariant] = []

        def on_response(response: Any) -> None:
            if _CARD_PATH_FRAGMENT in response.url:
                response_queue.put_nowait(response)

        page.on("response", on_response)
        url = f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            deadline = asyncio.get_running_loop().time() + 35
            while snapshot is None or not auth_seen:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                response = await asyncio.wait_for(response_queue.get(), timeout=remaining)
                headers = await response.request.all_headers()
                authorization = headers.get("authorization", "")
                auth_seen = auth_seen or authorization.lower().startswith("bearer ")
                try:
                    payload = await response.json()
                    parsed, parsed_variants = _parse_products(payload, "account_metadata")
                except Exception:
                    continue
                snapshot = parsed.get(nm_id) or snapshot
                variants = parsed_variants.get(nm_id) or variants
            selection = selection or VariantSelection()
            if selection.supplier_id is not None and snapshot.supplier_id != selection.supplier_id:
                raise AccountProductError(
                    f"Карточка {nm_id}: выбранный продавец больше не доступен"
                )
            if selection.option_id is not None:
                selected = next(
                    (item for item in variants if item.option_id == selection.option_id), None
                )
                if selected is None:
                    raise AccountProductError(f"Карточка {nm_id}: выбранный размер не найден")
                snapshot = selected.snapshot("account_metadata")
                if selection.size_name and len(variants) > 1:
                    size_nodes = page.locator(
                        '[class*="sizes-list"] button, [class*="sizesList"] button, '
                        '[data-testid*="size"]'
                    ).filter(has_text=selection.size_name)
                    if await size_nodes.count() == 0:
                        raise AccountProductError(
                            f"Карточка {nm_id}: размер {selection.size_name} нельзя выбрать в браузере"
                        )
                    await size_nodes.first.click(timeout=10_000)
                    await page.wait_for_timeout(1000)
            await page.wait_for_timeout(1500)
            dom = await page.evaluate(
                r"""
                () => {
                  const clean = (text) => {
                    const match = String(text || '').match(/(\d[\d\s\u00a0]{0,15})\s*[₽р]/i);
                    if (!match) return null;
                    const value = Number(match[1].replace(/\D/g, ''));
                    return Number.isFinite(value) && value > 0 ? value : null;
                  };
                  const first = (selectors) => {
                    for (const selector of selectors) {
                      const node = document.querySelector(selector);
                      const value = node ? clean(node.textContent) : null;
                      if (value) return value;
                    }
                    return null;
                  };
                  return {
                    wallet: first([
                      '[class*="walletPrice"]', '[class*="WalletPrice"]',
                      '[class*="wallet-price"]', '[data-testid*="wallet"]'
                    ]),
                    final: first([
                      '[class*="priceBlockFinalPrice"]', '[class*="finalPrice"]',
                      'ins[class*="priceBlock"]', '[data-testid*="price"]'
                    ])
                  };
                }
                """
            )
            wallet = dom.get("wallet") if isinstance(dom, dict) else None
            final = dom.get("final") if isinstance(dom, dict) else None
            displayed_rubles = wallet or final
            if not isinstance(displayed_rubles, int) or displayed_rubles <= 0:
                raise AccountProductError(f"Карточка {nm_id}: видимая персональная цена не найдена")
            snapshot = PriceSnapshot(
                nm_id=snapshot.nm_id,
                title=snapshot.title,
                brand=snapshot.brand,
                price=displayed_rubles * 100,
                basic_price=snapshot.basic_price,
                available=snapshot.available,
                quantity=snapshot.quantity,
                source="account_browser_wallet" if wallet else "account_browser",
                observed_at=utcnow(),
                option_id=snapshot.option_id,
                size_name=snapshot.size_name,
                supplier_id=snapshot.supplier_id,
                supplier_name=snapshot.supplier_name,
            )
            return snapshot, auth_seen
        except TimeoutError as exc:
            if snapshot is not None and not auth_seen:
                raise AccountSessionError("WB-сессия истекла: нет авторизованного запроса") from exc
            if "id.wb.ru" in page.url or "login" in page.url:
                raise AccountSessionError("WB перенаправил браузер на повторный вход") from exc
            raise AccountProductError(f"Карточка {nm_id}: тайм-аут или защитная проверка") from exc
        except Exception as exc:
            if isinstance(exc, (AccountSessionError, AccountProductError)):
                raise
            raise AccountProductError(
                f"Ошибка браузерной проверки WB для артикула {nm_id}: {type(exc).__name__}"
            ) from exc
        finally:
            with suppress(Exception):
                page.remove_listener("response", on_response)


def parse_price_text(text: str) -> int | None:
    match = _PRICE_TEXT_RE.search(text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) * 100 if digits else None


def parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
