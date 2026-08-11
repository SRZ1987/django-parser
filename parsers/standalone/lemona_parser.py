from __future__ import annotations

import asyncio
import random
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp

from .public_commerce_parser import (
    COLUMNS as SHARED_COLUMNS,
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


BASE_URL = "https://www.lemona.ee/"
MEDIA_BASE_URL = "https://www.lemona.lt/"
GRAPHQL_URL = "https://m2.lemona.lt/graphql"
LUPA_QUERY_URL = "https://api.lupasearch.com/v1/query/{query_key}"
STORE_CODE = "ee_ee"
PAGE_SIZE = 1000
GRAPHQL_BATCH_SIZE = 50
CONCURRENCY = 4
REQUEST_TIMEOUT = 60
QUANTITY_MIN_COLUMN = "Минимальное количество"
COLUMNS = [*SHARED_COLUMNS, QUANTITY_MIN_COLUMN]

STORE_CONFIG_QUERY = """
query StoreConfig {
  storeConfig {
    lupasearch_products_query_key
  }
}
"""

PRODUCT_DETAILS_QUERY = """
query ProductDetails($skus: [String!]!, $pageSize: Int!) {
  products(filter: {sku: {in: $skus}}, pageSize: $pageSize) {
    items {
      sku
      lemona_sku
      bkodai
      salable_qty
      supplier_qty
      stock_status
      price_tiers {
        quantity
        final_price { value currency }
      }
    }
  }
}
"""


async def request_json(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict[str, Any],
    *,
    label: str,
    headers: dict[str, str] | None = None,
    log_callback=None,
) -> dict[str, Any]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status == 200:
                    data = await response.json(content_type=None)
                    if not isinstance(data, dict):
                        raise RuntimeError(f"Lemona {label} returned non-object JSON.")
                    return data

                body = (await response.text())[:300]
                if response.status not in RETRYABLE_STATUSES or attempt >= MAX_RETRIES:
                    raise RuntimeError(
                        f"Lemona {label} failed with HTTP {response.status}: {body}"
                    )
                delay = retry_after_seconds(response.headers.get("Retry-After"))
                if delay is None:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
                log(
                    f"Lemona HTTP {response.status}: {label}, "
                    f"retry={attempt}/{MAX_RETRIES}, delay={delay:.1f}s",
                    log_callback,
                )
                await asyncio.sleep(delay)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt >= MAX_RETRIES:
                raise RuntimeError(f"Lemona {label} failed after retries: {exc}") from exc
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
            log(
                f"Lemona request error: {label}, retry={attempt}/{MAX_RETRIES}, "
                f"delay={delay:.1f}s, error={exc}",
                log_callback,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"Lemona {label} exhausted retries.")


def graphql_data(payload: dict[str, Any], label: str) -> dict[str, Any]:
    errors = payload.get("errors")
    if errors:
        messages = "; ".join(clean_text(error.get("message")) for error in errors if isinstance(error, dict))
        raise RuntimeError(f"Lemona {label} returned GraphQL errors: {messages or errors}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"Lemona {label} did not return GraphQL data.")
    return data


async def fetch_query_key(session: aiohttp.ClientSession, log_callback=None) -> str:
    payload = await request_json(
        session,
        GRAPHQL_URL,
        {"query": STORE_CONFIG_QUERY},
        headers={"Store": STORE_CODE},
        label="store configuration",
        log_callback=log_callback,
    )
    store_config = graphql_data(payload, "store configuration").get("storeConfig") or {}
    query_key = clean_text(store_config.get("lupasearch_products_query_key"))
    if not query_key:
        raise RuntimeError("Lemona store configuration has no public product query key.")
    log(f"Lemona catalog endpoint: {LUPA_QUERY_URL.format(query_key=query_key)}", log_callback)
    return query_key


def lupa_request(offset: int) -> dict[str, Any]:
    return {
        "searchText": "",
        "offset": offset,
        "limit": PAGE_SIZE,
        "sort": [{"sku": "asc"}],
        "filters": {
            "sources": {"exists": True},
            "price": {"gt": 0},
        },
    }


def lupa_items(payload: dict[str, Any], label: str) -> list[dict[str, Any]]:
    items = payload.get("items")
    if not isinstance(items, list):
        raise RuntimeError(f"Lemona {label} returned invalid product items.")
    return [item for item in items if isinstance(item, dict)]


async def fetch_catalog(session: aiohttp.ClientSession, query_key: str, log_callback=None):
    endpoint = LUPA_QUERY_URL.format(query_key=query_key)
    first_payload = await request_json(
        session,
        endpoint,
        lupa_request(0),
        label="catalog offset=0",
        log_callback=log_callback,
    )
    first_items = lupa_items(first_payload, "catalog offset=0")
    try:
        total = int(first_payload.get("total"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Lemona catalog did not return a valid total.") from exc
    if total <= 0 or not first_items:
        raise RuntimeError("Lemona public API returned an empty available catalog.")
    if total > 1_000_000:
        raise RuntimeError(
            f"Lemona filtered catalog is too large for safe pagination: products={total}."
        )

    offsets = list(range(PAGE_SIZE, total, PAGE_SIZE))
    pages: dict[int, list[dict[str, Any]]] = {0: first_items}
    limiter = asyncio.Semaphore(CONCURRENCY)
    completed = 1
    progress_lock = asyncio.Lock()
    log(f"Lemona available catalog: products={total}, pages={len(offsets) + 1}", log_callback)

    async def load_page(offset: int):
        nonlocal completed
        async with limiter:
            payload = await request_json(
                session,
                endpoint,
                lupa_request(offset),
                label=f"catalog offset={offset}",
                log_callback=log_callback,
            )
        items = lupa_items(payload, f"catalog offset={offset}")
        expected = min(PAGE_SIZE, total - offset)
        if len(items) != expected:
            raise RuntimeError(
                f"Lemona catalog page is incomplete: offset={offset}, "
                f"expected={expected}, received={len(items)}."
            )
        pages[offset] = items
        async with progress_lock:
            completed += 1
            log(
                f"Lemona download progress: pages={completed}/{len(offsets) + 1}, "
                f"products={sum(len(page) for page in pages.values())}/{total}",
                log_callback,
            )

    await asyncio.gather(*(load_page(offset) for offset in offsets))
    products = [item for offset in range(0, total, PAGE_SIZE) for item in pages[offset]]
    internal_skus = [clean_text(item.get("sku")) for item in products]
    if len(products) != total or any(not sku for sku in internal_skus):
        raise RuntimeError(
            f"Lemona catalog is incomplete: expected={total}, products={len(products)}."
        )
    if len(set(internal_skus)) != total:
        raise RuntimeError("Lemona catalog contains duplicate internal SKU values.")
    return products


async def fetch_product_details(
    session: aiohttp.ClientSession,
    internal_skus: list[str],
    log_callback=None,
) -> dict[str, dict[str, Any]]:
    batches = [
        internal_skus[start : start + GRAPHQL_BATCH_SIZE]
        for start in range(0, len(internal_skus), GRAPHQL_BATCH_SIZE)
    ]
    details: dict[str, dict[str, Any]] = {}
    limiter = asyncio.Semaphore(CONCURRENCY)
    completed = 0
    failed = 0
    progress_lock = asyncio.Lock()

    async def load_batch(index: int, batch: list[str]):
        nonlocal completed, failed
        try:
            async with limiter:
                payload = await request_json(
                    session,
                    GRAPHQL_URL,
                    {
                        "query": PRODUCT_DETAILS_QUERY,
                        "variables": {"skus": batch, "pageSize": len(batch)},
                    },
                    headers={"Store": STORE_CODE},
                    label=f"product details batch={index}",
                    log_callback=log_callback,
                )
            products = graphql_data(payload, f"product details batch={index}").get("products") or {}
            items = products.get("items") or []
            return {
                clean_text(item.get("sku")): item
                for item in items
                if isinstance(item, dict) and clean_text(item.get("sku"))
            }
        except Exception as exc:
            failed += 1
            log(
                f"WARNING: Lemona product detail batch {index} was skipped: {exc}",
                log_callback,
            )
            return {}
        finally:
            async with progress_lock:
                completed += 1
                if completed == len(batches) or completed % 20 == 0:
                    log(
                        f"Lemona detail progress: batches={completed}/{len(batches)}, "
                        f"failed={failed}",
                        log_callback,
                    )

    results = await asyncio.gather(
        *(load_batch(index, batch) for index, batch in enumerate(batches, start=1))
    )
    for result in results:
        details.update(result)
    return details


def primary_barcode(value: Any) -> str:
    for candidate in re.findall(r"\d{8,14}", clean_text(value)):
        barcode = normalize_barcode(candidate)
        if barcode:
            return barcode
    return ""


def category_fields(item: dict[str, Any]) -> tuple[str, str]:
    category_id = clean_text(item.get("category_id"))
    category_ids = [clean_text(value) for value in item.get("category_ids") or []]
    categories = [clean_text(value) for value in item.get("categories") or []]
    category_name = ""
    if category_id in category_ids:
        index = category_ids.index(category_id)
        if index < len(categories):
            category_name = categories[index]
    if not category_name and categories:
        category_name = categories[-1]
    return category_name, f"lemona-category-{category_id}" if category_id else ""


def quantity_price_fields(detail: dict[str, Any], current_price: float | str):
    valid_tiers = []
    for tier in detail.get("price_tiers") or []:
        if not isinstance(tier, dict):
            continue
        quantity = tier.get("quantity")
        final_price = parse_decimal_money((tier.get("final_price") or {}).get("value"))
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            continue
        if quantity <= 1 or final_price == "" or current_price == "" or final_price >= current_price:
            continue
        valid_tiers.append((quantity, final_price))
    return min(valid_tiers) if valid_tiers else ("", "")


def build_rows(products: list[dict[str, Any]], details: dict[str, dict[str, Any]]):
    rows = []
    skipped = 0
    for item in products:
        internal_sku = clean_text(item.get("sku"))
        detail = details.get(internal_sku, {})
        name = clean_text(item.get("title"))
        product_path = clean_text(item.get("url"))
        current_price = parse_decimal_money(item.get("price"))
        old_price = parse_decimal_money(item.get("old_price"))
        if not name or not internal_sku or not product_path or current_price == "":
            skipped += 1
            continue
        if clean_text(item.get("stock_status")).upper() not in {"", "IN_STOCK"}:
            skipped += 1
            continue

        if old_price != "" and current_price < old_price:
            price, sale_price = old_price, current_price
        else:
            price, sale_price = current_price, ""
        min_quantity, quantity_price = quantity_price_fields(detail, current_price)
        category_name, category_id = category_fields(item)
        image_path = clean_text(item.get("image_url"))
        public_sku = clean_text(detail.get("lemona_sku") or item.get("lemona_sku")) or internal_sku
        rows.append(
            [
                name,
                price,
                sale_price,
                quantity_price,
                primary_barcode(detail.get("bkodai")),
                f"lemona-{internal_sku}",
                urljoin(MEDIA_BASE_URL, image_path),
                urljoin(BASE_URL, product_path),
                public_sku,
                category_name,
                category_id,
                clean_text(item.get("description") or item.get("short_description")),
                min_quantity,
            ]
        )
    if not rows:
        raise RuntimeError("Lemona catalog has no valid products to export.")
    rows.sort(key=lambda row: (str(row[0]).casefold(), str(row[5])))
    return rows, skipped


async def main(output_path: str | Path, log_callback=None) -> None:
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, connect=15)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY)
    headers = {
        "Accept": "application/json",
        "Accept-Language": "et-EE,et;q=0.9,en;q=0.7",
        "User-Agent": "PriceCompareCatalogBot/1.0",
    }
    async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
        query_key = await fetch_query_key(session, log_callback)
        products = await fetch_catalog(session, query_key, log_callback)
        details = await fetch_product_details(
            session,
            [clean_text(item.get("sku")) for item in products],
            log_callback,
        )

    rows, skipped = build_rows(products, details)
    destination = Path(output_path)
    await asyncio.to_thread(save_excel, rows, destination, columns=COLUMNS)
    log(
        f"Lemona Excel created: path={destination}, products={len(rows)}, "
        f"skipped={skipped}, details={len(details)}",
        log_callback,
    )
