from __future__ import annotations

import asyncio
import hashlib
import html
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp
from openpyxl import Workbook


WORKSHEET_NAME = "Товары"
COLUMNS = [
    "Название товара",
    "Цена",
    "Цена со скидкой",
    "Цена со скидкой 2",
    "Штрихкод",
    "Код магазина",
    "Фото",
    "Ссылка",
    "SKU",
    "Категория",
    "Код категории",
    "Описание",
]

PAGE_SIZE = 100
SHOPIFY_PAGE_SIZE = 250
KLEVU_PAGE_SIZE = 100
CONCURRENCY = 4
REQUEST_TIMEOUT = 45
MAX_RETRIES = 5
RETRY_BASE_DELAY = 1.0
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
BARCODE_KEYS = {
    "barcode",
    "ean",
    "global_unique_id",
    "gtin",
    "gtin8",
    "gtin12",
    "gtin13",
    "gtin14",
}


@dataclass(frozen=True)
class CommerceStore:
    code: str
    name: str
    base_url: str
    platform: str = "woocommerce"
    enabled_by_default: bool = True
    api_url: str = ""
    api_key: str = ""

    @property
    def products_url(self) -> str:
        if self.platform == "klevu":
            return self.api_url
        if self.platform == "shopify":
            return f"{self.base_url}products.json"
        return f"{self.base_url}wp-json/wc/store/v1/products"


PUBLIC_COMMERCE_STORES = {
    store.code: store
    for store in (
        CommerceStore("emart", "Emart", "https://www.emart.ee/"),
        CommerceStore("nordhauser", "Nordhauser", "https://nordhauser.ee/"),
        CommerceStore("makserv", "Makserv", "https://www.makserv.ee/"),
        CommerceStore("ecopood", "Ecopood", "https://ecopood.ee/"),
        CommerceStore("tevokaup", "Tevo Ehituskaup", "https://www.tevokaup.ee/"),
        CommerceStore("vannitoapood", "Vannitoapood", "https://vannitoapood.ee/"),
        CommerceStore("tetko", "Tetko", "https://tetko.ee/"),
        CommerceStore("fastenerest", "FastenerEst", "https://www.fastenerest.ee/"),
        CommerceStore(
            "bestor",
            "Bestor",
            "https://bestor.ee/",
            enabled_by_default=False,
        ),
        CommerceStore("tooriistapood", "Tööriistapood", "https://www.tooriistapood.ee/"),
        CommerceStore("katus24", "Katus24", "https://katus24.ee/"),
        CommerceStore("ehitusoutlet", "Ehitusoutlet", "https://ehitusoutlet.ee/"),
        CommerceStore("hutton", "Hutton", "https://hutton.ee/"),
        CommerceStore("aquel", "Aquel", "https://aquel.ee/"),
        CommerceStore("ehitaks", "Ehitaks", "https://www.ehitaks.ee/"),
        CommerceStore(
            "katusemaailm",
            "Katusemaailm",
            "https://www.katusemaailm.ee/",
            enabled_by_default=False,
        ),
        CommerceStore("interstudio", "Interstudio", "https://interstudio.ee/"),
        CommerceStore("plaat24", "Plaat24", "https://www.plaat24.ee/"),
        CommerceStore("katusematerjal", "Katusematerjal", "https://katusematerjal.ee/"),
        CommerceStore(
            "horden",
            "Horden",
            "https://horden.ee/",
            platform="shopify",
            enabled_by_default=False,
        ),
        CommerceStore(
            "decora",
            "Decora",
            "https://www.decora.ee/",
            platform="klevu",
            api_url="https://decoracsv2.ksearchnet.com/cs/v2/search",
            api_key="klevu-159479682665411675",
        ),
    )
}


def log(message: str, callback=None) -> None:
    if callback:
        callback(message)
    else:
        print(message, flush=True)


def clean_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    value = html.unescape(str(value)).replace("\xa0", " ")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_barcode(value: Any) -> str:
    digits = re.sub(r"\D", "", clean_text(value))
    return digits if len(digits) in {8, 12, 13, 14} else ""


def extract_barcode(product: dict[str, Any]) -> str:
    def candidates(value: Any):
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in BARCODE_KEYS:
                    yield item
                if isinstance(item, (dict, list)):
                    yield from candidates(item)
        elif isinstance(value, list):
            for item in value:
                yield from candidates(item)

    for candidate in candidates(product):
        barcode = normalize_barcode(candidate)
        if barcode:
            return barcode
    return ""


def parse_minor_money(value: Any, minor_unit: int) -> float | str:
    if value in (None, "") or isinstance(value, bool):
        return ""
    try:
        return round(int(str(value)) / (10**minor_unit), minor_unit)
    except (TypeError, ValueError, OverflowError):
        return ""


def extract_woocommerce_prices(product: dict[str, Any]) -> tuple[float | str, float | str]:
    prices = product.get("prices") or {}
    try:
        minor_unit = min(max(int(prices.get("currency_minor_unit", 2)), 0), 6)
    except (TypeError, ValueError):
        minor_unit = 2

    current = parse_minor_money(prices.get("price"), minor_unit)
    regular = parse_minor_money(prices.get("regular_price"), minor_unit)
    sale = parse_minor_money(prices.get("sale_price"), minor_unit)
    if regular != "" and current != "" and current < regular:
        return regular, current
    if regular != "" and sale != "" and sale < regular:
        return regular, sale
    return current if current != "" else regular, ""


def parse_shopify_money(value: Any) -> float | str:
    if value in (None, "") or isinstance(value, bool):
        return ""
    try:
        return round(float(str(value)), 2)
    except (TypeError, ValueError, OverflowError):
        return ""


def parse_decimal_money(value: Any) -> float | str:
    if value in (None, "") or isinstance(value, bool):
        return ""
    try:
        price = round(float(str(value).replace(",", ".")), 2)
    except (TypeError, ValueError, OverflowError):
        return ""
    return price if price >= 0 else ""


def normalize_klevu_image_url(value: Any, base_url: str = "https://www.decora.ee/") -> str:
    image_url = clean_text(value)
    if not image_url:
        return ""
    image_url = image_url.replace("/needtochange/", "/", 1)
    if image_url.startswith("//"):
        return f"https:{image_url}"
    return urljoin(base_url, image_url)


def normalize_woocommerce_product(product: dict[str, Any]) -> list[Any] | None:
    if product.get("is_in_stock") is False:
        return None

    product_id = clean_text(product.get("id"))
    name = clean_text(product.get("name"))
    product_url = clean_text(product.get("permalink"))
    if not product_id or not name or not product_url:
        return None

    images = product.get("images") or []
    image_url = clean_text(images[0].get("src")) if images and isinstance(images[0], dict) else ""
    categories = [category for category in product.get("categories") or [] if isinstance(category, dict)]
    category = categories[-1] if categories else {}
    category_name = clean_text(category.get("name"))
    category_id = clean_text(category.get("id"))
    price, sale_price = extract_woocommerce_prices(product)
    if not any(value != "" and value > 0 for value in (price, sale_price)):
        return None
    return [
        name,
        price,
        sale_price,
        "",
        extract_barcode(product),
        f"wc-{product_id}",
        image_url,
        product_url,
        clean_text(product.get("sku")),
        category_name,
        f"wc-category-{category_id}" if category_id else "",
        clean_text(product.get("description") or product.get("short_description")),
    ]


def normalize_shopify_product(product: dict[str, Any], base_url: str) -> list[list[Any]]:
    product_id = clean_text(product.get("id"))
    title = clean_text(product.get("title"))
    handle = clean_text(product.get("handle"))
    if not product_id or not title or not handle:
        return []

    product_images = product.get("images") or []
    category_name = clean_text(product.get("product_type"))
    category_id = (
        f"shopify-type-{hashlib.sha256(category_name.casefold().encode('utf-8')).hexdigest()[:16]}"
        if category_name
        else ""
    )
    default_image = (
        clean_text(product_images[0].get("src"))
        if product_images and isinstance(product_images[0], dict)
        else ""
    )
    rows = []
    for variant in product.get("variants") or []:
        if not isinstance(variant, dict) or variant.get("available") is False:
            continue
        variant_id = clean_text(variant.get("id"))
        if not variant_id:
            continue

        variant_title = clean_text(variant.get("title"))
        name = title
        if variant_title and variant_title.casefold() != "default title":
            name = f"{title} - {variant_title}"

        current = parse_shopify_money(variant.get("price"))
        compare_at = parse_shopify_money(variant.get("compare_at_price"))
        if current != "" and compare_at != "" and current < compare_at:
            price, sale_price = compare_at, current
        else:
            price, sale_price = current, ""

        featured_image = variant.get("featured_image") or {}
        image_url = clean_text(featured_image.get("src")) if isinstance(featured_image, dict) else ""
        rows.append(
            [
                name,
                price,
                sale_price,
                "",
                normalize_barcode(variant.get("barcode")),
                f"shopify-{variant_id}",
                image_url or default_image,
                f"{base_url}products/{handle}?variant={variant_id}",
                clean_text(variant.get("sku")),
                category_name,
                category_id,
                clean_text(product.get("body_html")),
            ]
        )
    return rows


def normalize_klevu_product(product: dict[str, Any]) -> list[Any] | None:
    if clean_text(product.get("inStock")).casefold() in {"no", "false", "0"}:
        return None

    product_id = clean_text(product.get("id"))
    name = clean_text(product.get("name"))
    product_url = clean_text(product.get("url"))
    if not product_id or not name or not product_url:
        return None

    regular_price = parse_decimal_money(product.get("price"))
    sale_candidate = parse_decimal_money(product.get("salePrice"))
    if regular_price != "" and sale_candidate != "" and sale_candidate < regular_price:
        price, sale_price = regular_price, sale_candidate
    else:
        price = sale_candidate if sale_candidate != "" else regular_price
        sale_price = ""

    category_name = clean_text(product.get("category"))
    category_path = clean_text(product.get("klevu_category")) or category_name
    category_id = (
        f"klevu-category-{hashlib.sha256(category_path.casefold().encode('utf-8')).hexdigest()[:16]}"
        if category_path
        else ""
    )
    return [
        name,
        price,
        sale_price,
        "",
        extract_barcode(product),
        f"klevu-{product_id}",
        normalize_klevu_image_url(product.get("imageUrl") or product.get("image")),
        product_url,
        clean_text(product.get("sku")),
        category_name,
        category_id,
        clean_text(product.get("shortDesc")),
    ]


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0.0)
        except (TypeError, ValueError, OverflowError):
            return None


async def request_json(
    session: aiohttp.ClientSession,
    store: CommerceStore,
    *,
    params: dict[str, Any] | None = None,
    json_payload: dict[str, Any] | None = None,
    label: str,
    log_callback=None,
) -> tuple[Any, dict[str, str]]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            request_headers = None
            if store.platform == "klevu":
                request_headers = {
                    "Origin": store.base_url.rstrip("/"),
                    "Referer": store.base_url,
                    "User-Agent": "Mozilla/5.0 (compatible; PriceCompareCatalogBot/1.0)",
                }
            method = "POST" if json_payload is not None else "GET"
            async with session.request(
                method,
                store.products_url,
                params=params,
                json=json_payload,
                headers=request_headers,
            ) as response:
                status = response.status
                if status == 200:
                    try:
                        return await response.json(content_type=None), dict(response.headers)
                    except (ValueError, UnicodeDecodeError) as exc:
                        body = (await response.text())[:300]
                        raise RuntimeError(
                            f"{store.name} {label} returned invalid JSON: {body}"
                        ) from exc

                body = (await response.text())[:300]
                if status not in RETRYABLE_STATUSES or attempt >= MAX_RETRIES:
                    raise RuntimeError(f"{store.name} {label} failed with HTTP {status}: {body}")

                delay = retry_after_seconds(response.headers.get("Retry-After"))
                if delay is None:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
                log(
                    f"{store.name} HTTP {status}: {label}, retry={attempt}/{MAX_RETRIES}, delay={delay:.1f}s",
                    log_callback,
                )
                await asyncio.sleep(delay)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt >= MAX_RETRIES:
                raise RuntimeError(f"{store.name} {label} failed after retries: {exc}") from exc
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
            log(
                f"{store.name} request error: {label}, retry={attempt}/{MAX_RETRIES}, "
                f"delay={delay:.1f}s, error={exc}",
                log_callback,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"{store.name} {label} exhausted retries.")


def build_klevu_payload(store: CommerceStore, offset: int, limit: int) -> dict[str, Any]:
    return {
        "context": {"apiKeys": [store.api_key]},
        "recordQueries": [
            {
                "id": "productSearch",
                "typeOfRequest": "SEARCH",
                "settings": {
                    "query": {"term": "*"},
                    "typeOfRecords": ["KLEVU_PRODUCT"],
                    "limit": limit,
                    "offset": offset,
                },
            }
        ],
    }


def extract_klevu_result(payload: Any, store: CommerceStore) -> dict[str, Any]:
    results = payload.get("queryResults") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise RuntimeError(f"{store.name} Klevu response does not contain queryResults.")
    return results[0]


def extract_klevu_records(payload: Any, store: CommerceStore) -> list[dict[str, Any]]:
    records = extract_klevu_result(payload, store).get("records")
    if not isinstance(records, list):
        raise RuntimeError(f"{store.name} Klevu response contains invalid records.")
    return [record for record in records if isinstance(record, dict)]


def extract_klevu_total(payload: Any, store: CommerceStore) -> int:
    result = extract_klevu_result(payload, store)
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    for value in (result.get("totalResultsFound"), meta.get("totalResultsFound")):
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return len(extract_klevu_records(payload, store))


async def fetch_woocommerce_catalog(
    session: aiohttp.ClientSession,
    store: CommerceStore,
    log_callback=None,
) -> list[dict[str, Any]]:
    base_params = {"per_page": PAGE_SIZE, "orderby": "id", "order": "asc"}
    first_page, headers = await request_json(
        session,
        store,
        params={**base_params, "page": 1},
        label="page=1",
        log_callback=log_callback,
    )
    if not isinstance(first_page, list):
        raise RuntimeError(f"{store.name} API page 1 returned non-list JSON.")
    try:
        total = int(headers["X-WP-Total"])
        total_pages = int(headers["X-WP-TotalPages"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"{store.name} API did not return catalog pagination headers.") from exc
    if total <= 0 or total_pages <= 0 or not first_page:
        raise RuntimeError(f"{store.name} API returned an empty catalog.")

    log(
        f"{store.name} API: endpoint={store.products_url}, status=200, products={total}, pages={total_pages}",
        log_callback,
    )
    pages: dict[int, list[dict[str, Any]]] = {1: first_page}
    completed = 1
    progress_lock = asyncio.Lock()
    limiter = asyncio.Semaphore(CONCURRENCY)

    async def load_page(page: int) -> None:
        nonlocal completed
        async with limiter:
            records, _headers = await request_json(
                session,
                store,
                params={**base_params, "page": page},
                label=f"page={page}",
                log_callback=log_callback,
            )
        if not isinstance(records, list):
            raise RuntimeError(f"{store.name} API page {page} returned non-list JSON.")
        pages[page] = records
        async with progress_lock:
            completed += 1
            if completed == total_pages or completed % 10 == 0:
                downloaded = sum(len(items) for items in pages.values())
                log(
                    f"{store.name} download progress: pages={completed}/{total_pages}, "
                    f"products={downloaded}/{total}",
                    log_callback,
                )

    await asyncio.gather(*(load_page(page) for page in range(2, total_pages + 1)))
    products = [product for page in range(1, total_pages + 1) for product in pages[page]]
    product_ids = {str(product.get("id")) for product in products if product.get("id") not in (None, "")}
    if len(product_ids) < total:
        raise RuntimeError(
            f"{store.name} catalog is incomplete: expected={total}, unique_products={len(product_ids)}."
        )
    return products


async def fetch_shopify_catalog(
    session: aiohttp.ClientSession,
    store: CommerceStore,
    log_callback=None,
) -> list[dict[str, Any]]:
    products = []
    page = 1
    while True:
        payload, _headers = await request_json(
            session,
            store,
            params={"limit": SHOPIFY_PAGE_SIZE, "page": page},
            label=f"page={page}",
            log_callback=log_callback,
        )
        page_products = payload.get("products") if isinstance(payload, dict) else None
        if not isinstance(page_products, list):
            raise RuntimeError(f"{store.name} Shopify page {page} returned invalid product data.")
        if not page_products:
            break
        products.extend(page_products)
        if page == 1 or page % 5 == 0 or len(page_products) < SHOPIFY_PAGE_SIZE:
            log(
                f"{store.name} download progress: pages={page}, products={len(products)}",
                log_callback,
            )
        if len(page_products) < SHOPIFY_PAGE_SIZE:
            break
        page += 1
        if page > 10000:
            raise RuntimeError(f"{store.name} Shopify pagination exceeded its safety limit.")

    if not products:
        raise RuntimeError(f"{store.name} Shopify API returned an empty catalog.")
    product_ids = [str(product.get("id")) for product in products if product.get("id") not in (None, "")]
    if len(product_ids) != len(set(product_ids)):
        raise RuntimeError(f"{store.name} Shopify API returned duplicate product IDs.")
    return products


async def fetch_klevu_catalog(
    session: aiohttp.ClientSession,
    store: CommerceStore,
    log_callback=None,
) -> list[dict[str, Any]]:
    first_payload, _headers = await request_json(
        session,
        store,
        json_payload=build_klevu_payload(store, 0, KLEVU_PAGE_SIZE),
        label="offset=0",
        log_callback=log_callback,
    )
    first_records = extract_klevu_records(first_payload, store)
    total = extract_klevu_total(first_payload, store)
    if total <= 0 or not first_records:
        raise RuntimeError(f"{store.name} Klevu API returned an empty catalog.")

    log(
        f"{store.name} API: endpoint={store.products_url}, status=200, products={total}",
        log_callback,
    )
    pages: dict[int, list[dict[str, Any]]] = {0: first_records}
    completed = 1
    progress_lock = asyncio.Lock()
    limiter = asyncio.Semaphore(CONCURRENCY)
    offsets = list(range(KLEVU_PAGE_SIZE, total, KLEVU_PAGE_SIZE))

    async def load_offset(offset: int) -> None:
        nonlocal completed
        limit = min(KLEVU_PAGE_SIZE, total - offset)
        async with limiter:
            payload, _page_headers = await request_json(
                session,
                store,
                json_payload=build_klevu_payload(store, offset, limit),
                label=f"offset={offset}",
                log_callback=log_callback,
            )
        records = extract_klevu_records(payload, store)
        if len(records) != limit:
            raise RuntimeError(
                f"{store.name} Klevu page is incomplete: offset={offset}, "
                f"expected={limit}, received={len(records)}."
            )
        pages[offset] = records
        async with progress_lock:
            completed += 1
            if completed == len(offsets) + 1 or completed % 20 == 0:
                downloaded = sum(len(items) for items in pages.values())
                log(
                    f"{store.name} download progress: pages={completed}/{len(offsets) + 1}, "
                    f"products={downloaded}/{total}",
                    log_callback,
                )

    await asyncio.gather(*(load_offset(offset) for offset in offsets))
    products = [product for offset in range(0, total, KLEVU_PAGE_SIZE) for product in pages[offset]]
    product_ids = {
        clean_text(product.get("id"))
        for product in products
        if clean_text(product.get("id"))
    }
    if len(products) != total or len(product_ids) != total:
        raise RuntimeError(
            f"{store.name} catalog is incomplete: expected={total}, "
            f"products={len(products)}, unique_products={len(product_ids)}."
        )
    return products


def build_rows(store: CommerceStore, products: list[dict[str, Any]]) -> tuple[list[list[Any]], int]:
    rows_by_external_id: dict[str, list[Any]] = {}
    skipped = 0
    for product in products:
        if store.platform == "klevu":
            row = normalize_klevu_product(product)
            product_rows = [row] if row is not None else []
            if row is None:
                skipped += 1
        elif store.platform == "shopify":
            product_rows = normalize_shopify_product(product, store.base_url)
            if not product_rows:
                skipped += 1
        else:
            row = normalize_woocommerce_product(product)
            product_rows = [row] if row is not None else []
            if row is None:
                skipped += 1

        for row in product_rows:
            external_id = row[5]
            existing = rows_by_external_id.get(external_id)
            if existing is not None and existing[7] != row[7]:
                raise RuntimeError(
                    f"{store.name} catalog contains duplicate external_id {external_id!r} "
                    "for different product URLs."
                )
            rows_by_external_id[external_id] = row

    rows = sorted(rows_by_external_id.values(), key=lambda item: (str(item[0]).casefold(), str(item[5])))
    if not rows:
        raise RuntimeError(f"{store.name} catalog has no available products to export.")
    return rows, skipped


def save_excel(
    rows: list[list[Any]],
    output_path: Path,
    *,
    columns: list[str] | None = None,
) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = WORKSHEET_NAME
    worksheet.append(columns or COLUMNS)
    for row in rows:
        worksheet.append(row)

    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    widths = {
        "A": 65,
        "B": 14,
        "C": 18,
        "D": 20,
        "E": 22,
        "F": 24,
        "G": 70,
        "H": 80,
        "I": 24,
        "J": 35,
        "K": 24,
        "L": 80,
        "M": 24,
    }
    for column, width in widths.items():
        worksheet.column_dimensions[column].width = width
    for row_number in range(2, worksheet.max_row + 1):
        for column in ("E", "F", "I"):
            worksheet[f"{column}{row_number}"].number_format = "@"
        for column in ("B", "C", "D"):
            worksheet[f"{column}{row_number}"].number_format = "0.00"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)
    workbook.close()


async def main(store_code: str, output_path: str | Path, log_callback=None) -> None:
    try:
        store = PUBLIC_COMMERCE_STORES[store_code]
    except KeyError as exc:
        raise ValueError(f"Unknown public commerce store: {store_code}") from exc

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, connect=15)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY)
    headers = {
        "Accept": "application/json",
        "Accept-Language": "et-EE,et;q=0.9,en;q=0.7",
        "User-Agent": "PriceCompareCatalogBot/1.0",
    }
    async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
        if store.platform == "klevu":
            products = await fetch_klevu_catalog(session, store, log_callback)
        elif store.platform == "shopify":
            products = await fetch_shopify_catalog(session, store, log_callback)
        else:
            products = await fetch_woocommerce_catalog(session, store, log_callback)

    rows, skipped = build_rows(store, products)
    destination = Path(output_path)
    await asyncio.to_thread(save_excel, rows, destination)
    log(
        f"{store.name} Excel created: path={destination}, products={len(rows)}, skipped={skipped}",
        log_callback,
    )
