from __future__ import annotations

import asyncio
import hashlib
import random
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from .public_commerce_parser import (
    COLUMNS,
    MAX_RETRIES,
    RETRYABLE_STATUSES,
    RETRY_BASE_DELAY,
    WORKSHEET_NAME,
    clean_text,
    log,
    normalize_barcode,
    parse_decimal_money,
    retry_after_seconds,
    save_excel,
)


REQUEST_TIMEOUT = 150
STOKKER_PAGE_SIZE = 1000
STOKKER_PAGE_BATCH = 4
STOKKER_MAX_PAGES = 100
ESVIKA_PAGE_SIZE = 1000
MIN_EXPORTED_PRODUCTS = {"stokker": 10000, "esvika": 1000}


@dataclass(frozen=True)
class ApiRetailer:
    code: str
    name: str
    base_url: str
    api_url: str


API_RETAILERS = {
    store.code: store
    for store in (
        ApiRetailer(
            code="stokker",
            name="Stokker",
            base_url="https://www.stokker.ee/",
            api_url="https://api.stokker.com/products/get",
        ),
        ApiRetailer(
            code="esvika",
            name="Esvika",
            base_url="https://pood.esvika.ee/",
            api_url="https://pood.esvika.ee/api/catalog/get-products",
        ),
    )
}


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _category_id(store_code: str, value: Any) -> str:
    text = clean_text(value).casefold()
    return f"{store_code}-category-{_stable_hash(text)}" if text else ""


async def request_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict[str, Any],
    label: str,
    store: ApiRetailer,
    log_callback=None,
) -> Any:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    return await response.json(content_type=None)

                body = (await response.text())[:300]
                if response.status not in RETRYABLE_STATUSES or attempt >= MAX_RETRIES:
                    raise RuntimeError(
                        f"{store.name} {label} failed with HTTP {response.status}: {body}"
                    )
                delay = retry_after_seconds(response.headers.get("Retry-After"))
                if delay is None:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
                log(
                    f"{store.name} HTTP {response.status}: {label}, "
                    f"retry={attempt}/{MAX_RETRIES}, delay={delay:.1f}s",
                    log_callback,
                )
                await asyncio.sleep(delay)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    f"{store.name} {label} failed after retries: {exc}"
                ) from exc
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
            log(
                f"{store.name} request error: {label}, retry={attempt}/{MAX_RETRIES}, "
                f"delay={delay:.1f}s, error={exc}",
                log_callback,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"{store.name} {label} exhausted retries.")


def normalize_stokker_product(product: dict[str, Any]) -> list[Any] | None:
    item_id = clean_text(product.get("ItemID"))
    name = clean_text(product.get("NameC") or product.get("Name"))
    if not item_id or not name or product.get("CanBuy") is False:
        return None

    regular_price = parse_decimal_money(product.get("PriceWithVat"))
    customer_price = parse_decimal_money(product.get("CustomerPriceWithVat"))
    if regular_price == "" and customer_price == "":
        return None
    if regular_price != "" and customer_price != "" and customer_price < regular_price:
        price, sale_price = regular_price, customer_price
    else:
        price, sale_price = customer_price if customer_price != "" else regular_price, ""

    category_name = clean_text(product.get("CategoryName"))
    category_code = clean_text(product.get("CategoryCode") or product.get("Category"))
    product_url = clean_text(product.get("LinkToProducts"))
    if not product_url:
        product_url = urljoin("https://www.stokker.com/et/", clean_text(product.get("Path")))

    return [
        name,
        price,
        sale_price,
        "",
        normalize_barcode(product.get("ItemBarcode")),
        f"stokker-{item_id}",
        clean_text(product.get("ImageL") or product.get("ImageM")),
        product_url,
        item_id,
        category_name,
        f"stokker-category-{category_code}" if category_code else _category_id("stokker", category_name),
        clean_text(product.get("Description") or product.get("ProductSpec")),
    ]


def _esvika_in_stock(product: dict[str, Any]) -> bool:
    for availability in product.get("availabilities") or []:
        if not isinstance(availability, dict):
            continue
        try:
            if float(availability.get("inventoryAmountValue") or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def normalize_esvika_product(product: dict[str, Any], base_url: str) -> list[Any] | None:
    product_id = clean_text(product.get("id"))
    name = clean_text(product.get("productName"))
    product_path = clean_text(product.get("url"))
    price = parse_decimal_money(product.get("priceIncludingVAT"))
    if (
        not product_id
        or not name
        or not product_path
        or price == ""
        or product.get("askPrice") is True
        or not _esvika_in_stock(product)
    ):
        return None

    path_parts = [part for part in urlparse(product_path).path.split("/") if part]
    category_slug = path_parts[0] if path_parts else ""
    category_name = category_slug.replace("-", " ").strip().title()
    picture_id = clean_text(product.get("pictureId"))
    return [
        name,
        price,
        "",
        "",
        "",
        f"esvika-{product_id}",
        urljoin(base_url, f"ProductPicture/{picture_id}") if picture_id else "",
        urljoin(base_url, product_path),
        product_id,
        category_name,
        f"esvika-category-{category_slug}" if category_slug else "",
        clean_text(product.get("supplierDescription")),
    ]


async def fetch_stokker_products(
    session: aiohttp.ClientSession,
    store: ApiRetailer,
    log_callback=None,
) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for first_page in range(1, STOKKER_MAX_PAGES + 1, STOKKER_PAGE_BATCH):
        pages = list(
            range(first_page, min(first_page + STOKKER_PAGE_BATCH, STOKKER_MAX_PAGES + 1))
        )
        responses = await asyncio.gather(
            *(
                request_json(
                    session,
                    store.api_url,
                    params={
                        "lang": "et",
                        "country": "EE",
                        "DataAreaID": "SET",
                        "type": "sitemap",
                        "limit": STOKKER_PAGE_SIZE,
                        "page": page,
                    },
                    label=f"catalog page {page}",
                    store=store,
                    log_callback=log_callback,
                )
                for page in pages
            )
        )
        reached_end = False
        for page, payload in zip(pages, responses):
            if not isinstance(payload, list):
                raise RuntimeError(f"Stokker catalog page {page} returned invalid JSON.")
            products.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < STOKKER_PAGE_SIZE:
                reached_end = True
                break
        log(
            f"Stokker download progress: pages={pages[0]}-{pages[-1]}, "
            f"products={len(products)}",
            log_callback,
        )
        if reached_end:
            return products
    raise RuntimeError(
        f"Stokker catalog exceeded the safety limit of {STOKKER_MAX_PAGES} pages."
    )


async def fetch_esvika_products(
    session: aiohttp.ClientSession,
    store: ApiRetailer,
    log_callback=None,
) -> list[dict[str, Any]]:
    first = await request_json(
        session,
        store.api_url,
        params={"page": 1, "pageSize": ESVIKA_PAGE_SIZE},
        label="catalog page 1",
        store=store,
        log_callback=log_callback,
    )
    if not isinstance(first, dict) or not isinstance(first.get("items"), list):
        raise RuntimeError("Esvika catalog returned invalid JSON.")
    try:
        total = int(first.get("total") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Esvika catalog did not provide a valid total count.") from exc
    if total <= 0:
        raise RuntimeError("Esvika catalog is empty.")

    page_count = ceil(total / ESVIKA_PAGE_SIZE)
    remaining = await asyncio.gather(
        *(
            request_json(
                session,
                store.api_url,
                params={"page": page, "pageSize": ESVIKA_PAGE_SIZE},
                label=f"catalog page {page}",
                store=store,
                log_callback=log_callback,
            )
            for page in range(2, page_count + 1)
        )
    )
    products = [item for item in first["items"] if isinstance(item, dict)]
    for page, payload in enumerate(remaining, start=2):
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise RuntimeError(f"Esvika catalog page {page} returned invalid JSON.")
        products.extend(item for item in payload["items"] if isinstance(item, dict))
        if page == page_count or page % 5 == 0:
            log(
                f"Esvika download progress: pages={page}/{page_count}, products={len(products)}",
                log_callback,
            )
    if len(products) != total:
        raise RuntimeError(
            f"Esvika catalog is incomplete: expected={total}, received={len(products)}."
        )
    return products


def build_rows(store: ApiRetailer, products: list[dict[str, Any]]) -> tuple[list[list[Any]], int]:
    rows_by_id: dict[str, list[Any]] = {}
    skipped = 0
    for product in products:
        if store.code == "stokker":
            row = normalize_stokker_product(product)
        else:
            row = normalize_esvika_product(product, store.base_url)
        if row is None:
            skipped += 1
            continue
        external_id = row[5]
        existing = rows_by_id.get(external_id)
        if existing is not None and existing[7] != row[7]:
            raise RuntimeError(
                f"{store.name} duplicate external_id {external_id!r} has different URLs."
            )
        rows_by_id[external_id] = row
    rows = sorted(rows_by_id.values(), key=lambda row: (str(row[0]).casefold(), str(row[5])))
    minimum = MIN_EXPORTED_PRODUCTS[store.code]
    if len(rows) < minimum:
        raise RuntimeError(
            f"{store.name} catalog is unexpectedly small: products={len(rows)}, "
            f"minimum={minimum}."
        )
    return rows, skipped


async def main(store_code: str, output_path: str | Path, log_callback=None) -> None:
    try:
        store = API_RETAILERS[store_code]
    except KeyError as exc:
        raise ValueError(f"Unknown API retailer: {store_code}") from exc

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, connect=20)
    connector = aiohttp.TCPConnector(limit=STOKKER_PAGE_BATCH)
    headers = {
        "Accept": "application/json",
        "Accept-Language": "et-EE,et;q=0.9,en;q=0.7",
        "User-Agent": "PriceCompareCatalogBot/1.0",
    }
    if store.code == "stokker":
        headers["X-Language"] = "et"

    async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
        if store.code == "stokker":
            products = await fetch_stokker_products(session, store, log_callback)
        else:
            products = await fetch_esvika_products(session, store, log_callback)

    rows, skipped = build_rows(store, products)
    destination = Path(output_path)
    await asyncio.to_thread(save_excel, rows, destination)
    log(
        f"{store.name} Excel created: path={destination}, products={len(rows)}, skipped={skipped}",
        log_callback,
    )
