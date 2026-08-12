from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from xml.etree import ElementTree

import aiohttp

from .public_commerce_parser import (
    COLUMNS,
    MAX_RETRIES,
    RETRYABLE_STATUSES,
    RETRY_BASE_DELAY,
    WORKSHEET_NAME,
    clean_text,
    log,
    parse_decimal_money,
    retry_after_seconds,
    save_excel,
)


BASE_URL = "https://www.motonet.ee/"
CATEGORY_SITEMAP_URL = f"{BASE_URL}sitemaps/categories.xml"
PAGE_SIZE = 30
BATCH_SIZE = 100
CONCURRENCY = 5
REQUEST_TIMEOUT = 45
MAX_TOLERATED_MISSING_AVAILABILITY = 10
MAX_TOLERATED_MISSING_AVAILABILITY_RATIO = 0.001


def parse_top_categories(xml_text: str) -> list[dict[str, str]]:
    root = ElementTree.fromstring(xml_text)
    categories = []
    for node in root.iter():
        if not node.tag.endswith("loc") or not node.text:
            continue
        url = clean_text(node.text)
        parsed = urlparse(url)
        prefix = "/tooteruhmad/"
        if not parsed.path.startswith(prefix):
            continue
        path = parsed.path.rstrip("/")
        slug = path[len(prefix) :]
        if not slug or "/" in slug:
            continue
        category_id = clean_text(parse_qs(parsed.query).get("category", [""])[0])
        if category_id:
            categories.append(
                {
                    "id": category_id,
                    "slug": slug,
                    "url": url,
                }
            )
    unique = {category["id"]: category for category in categories}
    return list(unique.values())


async def request(
    session: aiohttp.ClientSession,
    url: str,
    *,
    label: str,
    json_payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    expect_json: bool = True,
    log_callback=None,
):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            method = "POST" if json_payload is not None else "GET"
            async with session.request(
                method,
                url,
                json=json_payload,
                headers=headers,
            ) as response:
                if response.status == 200:
                    if expect_json:
                        return await response.json(content_type=None)
                    return await response.text()

                body = (await response.text())[:300]
                if response.status not in RETRYABLE_STATUSES or attempt >= MAX_RETRIES:
                    raise RuntimeError(f"Motonet {label} failed with HTTP {response.status}: {body}")

                delay = retry_after_seconds(response.headers.get("Retry-After"))
                if delay is None:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
                log(
                    f"Motonet HTTP {response.status}: {label}, "
                    f"retry={attempt}/{MAX_RETRIES}, delay={delay:.1f}s",
                    log_callback,
                )
                await asyncio.sleep(delay)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt >= MAX_RETRIES:
                raise RuntimeError(f"Motonet {label} failed after retries: {exc}") from exc
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
            log(
                f"Motonet request error: {label}, retry={attempt}/{MAX_RETRIES}, "
                f"delay={delay:.1f}s, error={exc}",
                log_callback,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"Motonet {label} exhausted retries.")


def category_headers(category: dict[str, str]) -> dict[str, str]:
    return {
        "attribute-alias-value": category["slug"],
        "attribute-alias-details": category["slug"],
        "Referer": category["url"],
    }


async def fetch_category_page(
    session: aiohttp.ClientSession,
    category: dict[str, str],
    page: int,
    *,
    limiter: asyncio.Semaphore,
    log_callback=None,
) -> dict[str, Any]:
    url = f"{BASE_URL}api/search/categories/{category['id']}/products?locale=et"
    async with limiter:
        payload = await request(
            session,
            url,
            label=f"category={category['slug']} page={page}",
            json_payload={"page": page, "pageSize": PAGE_SIZE},
            headers=category_headers(category),
            log_callback=log_callback,
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
        raise RuntimeError(
            f"Motonet category={category['slug']} page={page} returned invalid data."
        )
    return payload


async def fetch_catalog(session: aiohttp.ClientSession, log_callback=None) -> list[dict[str, Any]]:
    sitemap_xml = await request(
        session,
        CATEGORY_SITEMAP_URL,
        label="category sitemap",
        expect_json=False,
        log_callback=log_callback,
    )
    categories = parse_top_categories(sitemap_xml)
    if not categories:
        raise RuntimeError("Motonet category sitemap contains no top-level categories.")
    log(f"Motonet catalog source: categories={len(categories)}", log_callback)

    limiter = asyncio.Semaphore(CONCURRENCY)
    first_pages = await asyncio.gather(
        *(
            fetch_category_page(
                session,
                category,
                1,
                limiter=limiter,
                log_callback=log_callback,
            )
            for category in categories
        )
    )
    page_jobs = []
    total_expected = 0
    for category, payload in zip(categories, first_pages):
        pagination = payload.get("pagination") or {}
        page_count = max(int(pagination.get("pageCount") or 1), 1)
        total_expected += max(int(pagination.get("totalCount") or 0), 0)
        for page in range(2, page_count + 1):
            page_jobs.append((category, page))

    completed_pages = len(first_pages)
    all_page_count = completed_pages + len(page_jobs)

    async def fetch_page(category, page):
        nonlocal completed_pages
        payload = await fetch_category_page(
            session,
            category,
            page,
            limiter=limiter,
            log_callback=log_callback,
        )
        completed_pages += 1
        if completed_pages == all_page_count or completed_pages % 100 == 0:
            log(
                f"Motonet download progress: pages={completed_pages}/{all_page_count}",
                log_callback,
            )
        return payload

    remaining_pages = await asyncio.gather(
        *(fetch_page(category, page) for category, page in page_jobs)
    )
    products: dict[str, dict[str, Any]] = {}
    for payload in [*first_pages, *remaining_pages]:
        for product in payload["products"]:
            if not isinstance(product, dict):
                continue
            product_code = clean_text(product.get("id"))
            if product_code:
                products.setdefault(product_code, product)

    if not products:
        raise RuntimeError("Motonet API returned an empty catalog.")
    log(
        f"Motonet catalog downloaded: category_rows={total_expected}, "
        f"unique_products={len(products)}, pages={all_page_count}",
        log_callback,
    )
    return list(products.values())


def chunks(values: list[str], size: int = BATCH_SIZE):
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


async def fetch_batch_data(
    session: aiohttp.ClientSession,
    product_codes: list[str],
    *,
    endpoint: str,
    payload_key: str,
    label: str,
    log_callback=None,
) -> list[dict[str, Any]]:
    limiter = asyncio.Semaphore(CONCURRENCY)
    batches = list(chunks(product_codes))
    completed = 0

    async def fetch_one(index, batch):
        nonlocal completed
        async with limiter:
            payload = await request(
                session,
                f"{BASE_URL}api/{endpoint}",
                label=f"{label} batch {index}/{len(batches)}",
                json_payload={payload_key: batch},
                log_callback=log_callback,
            )
        completed += 1
        if completed == len(batches) or completed % 50 == 0:
            log(
                f"Motonet {label} progress: batches={completed}/{len(batches)}",
                log_callback,
            )
        if not isinstance(payload, list):
            raise RuntimeError(f"Motonet {label} endpoint returned invalid data.")
        return payload

    results = await asyncio.gather(
        *(fetch_one(index, batch) for index, batch in enumerate(batches, start=1))
    )
    return [item for result in results for item in result if isinstance(item, dict)]


def availability_by_code(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        clean_text(item.get("productCode")): item
        for item in items
        if clean_text(item.get("productCode"))
    }


async def recover_missing_availability(
    session: aiohttp.ClientSession,
    product_codes: list[str],
    availability_items: list[dict[str, Any]],
    *,
    log_callback=None,
) -> dict[str, dict[str, Any]]:
    availability = availability_by_code(availability_items)
    missing = sorted(set(product_codes) - set(availability))
    if not missing:
        return availability

    log(
        f"WARNING: Motonet availability response omitted {len(missing)} products; "
        "retrying only the missing product codes.",
        log_callback,
    )
    try:
        retry_items = await fetch_batch_data(
            session,
            missing,
            endpoint="stocksAndAvailability/availabilities",
            payload_key="productCodes",
            label="availability recovery",
            log_callback=log_callback,
        )
    except RuntimeError as exc:
        log(
            f"WARNING: Motonet availability recovery failed: {exc}",
            log_callback,
        )
    else:
        availability.update(availability_by_code(retry_items))
    return availability


def validate_availability_coverage(
    product_codes: list[str],
    availability: dict[str, dict[str, Any]],
    *,
    log_callback=None,
) -> list[str]:
    missing = sorted(set(product_codes) - set(availability))
    if not missing:
        return []

    missing_ratio = len(missing) / max(len(set(product_codes)), 1)
    if (
        len(missing) > MAX_TOLERATED_MISSING_AVAILABILITY
        or missing_ratio > MAX_TOLERATED_MISSING_AVAILABILITY_RATIO
    ):
        raise RuntimeError(
            "Motonet availability response is incomplete: "
            f"missing={len(missing)}, ratio={missing_ratio:.4%}."
        )

    sample = ", ".join(missing[:5])
    log(
        "WARNING: Motonet availability remains missing for "
        f"{len(missing)}/{len(set(product_codes))} products after recovery; "
        f"treating them as unavailable. sample={sample}",
        log_callback,
    )
    return missing


def prices_by_code(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for item in items:
        product = item.get("product") or {}
        product_code = clean_text(product.get("productCode"))
        if product_code:
            result[product_code] = item
    return result


def _find_discounted_price(value: Any) -> float | str:
    if isinstance(value, dict):
        for key in ("discountedPrice", "campaignPrice", "finalPrice"):
            price = parse_decimal_money(value.get(key))
            if price != "":
                return price
        for child in value.values():
            price = _find_discounted_price(child)
            if price != "":
                return price
    elif isinstance(value, list):
        for child in value:
            price = _find_discounted_price(child)
            if price != "":
                return price
    return ""


def extract_prices(price_data: dict[str, Any], fallback: Any = "") -> tuple[float | str, float | str]:
    price_block = price_data.get("price") or {}
    regular_price = parse_decimal_money(price_block.get("price"))
    if regular_price == "":
        regular_price = parse_decimal_money(fallback)
    discounted_price = _find_discounted_price(price_data.get("campaign"))
    if (
        regular_price != ""
        and discounted_price != ""
        and discounted_price < regular_price
    ):
        return regular_price, discounted_price
    return regular_price, ""


def category_id(product: dict[str, Any]) -> str:
    category_url = clean_text(product.get("categoryUrl"))
    if not category_url:
        return ""
    parsed = urlparse(category_url)
    value = clean_text(parse_qs(parsed.query).get("category", [""])[0])
    return f"motonet-category-{value}" if value else ""


def build_rows(
    products: list[dict[str, Any]],
    availability: dict[str, dict[str, Any]],
    prices: dict[str, dict[str, Any]],
) -> tuple[list[list[Any]], int]:
    rows = []
    skipped = 0
    for product in products:
        product_code = clean_text(product.get("id"))
        stock = availability.get(product_code)
        is_available = bool(
            stock
            and (
                stock.get("webstoreDeliverable")
                or stock.get("orderableToLocations")
                or stock.get("locations")
            )
        )
        name = clean_text(product.get("name"))
        price, sale_price = extract_prices(prices.get(product_code, {}), product.get("price"))
        if not product_code or not name or not is_available or price == "":
            skipped += 1
            continue

        encoded_code = quote(product_code, safe="")
        rows.append([
            name,
            price,
            sale_price,
            "",
            "",
            f"motonet-{product_code}",
            f"https://cdn.broman.group/api/image/v2/image/eesti/productcode/"
            f"{encoded_code}/600/600/80/FFFFFF00.jpg",
            f"{BASE_URL}toode/product?product={encoded_code}",
            product_code,
            clean_text(product.get("categoryName")),
            category_id(product),
            clean_text(product.get("description")),
        ])
    return rows, skipped


async def main(output_path: str | Path, log_callback=None) -> None:
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, connect=15)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "et-EE,et;q=0.9,en;q=0.7",
        "Content-Type": "application/json",
        "Origin": BASE_URL.rstrip("/"),
        "Referer": BASE_URL,
        "User-Agent": "PriceCompareCatalogBot/1.0",
    }
    async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
        products = await fetch_catalog(session, log_callback)
        product_codes = [clean_text(product.get("id")) for product in products]
        availability_items, price_items = await asyncio.gather(
            fetch_batch_data(
                session,
                product_codes,
                endpoint="stocksAndAvailability/availabilities",
                payload_key="productCodes",
                label="availability",
                log_callback=log_callback,
            ),
            fetch_batch_data(
                session,
                product_codes,
                endpoint="pricing/prices?locale=et",
                payload_key="productCodeList",
                label="prices",
                log_callback=log_callback,
            ),
        )
        availability = await recover_missing_availability(
            session,
            product_codes,
            availability_items,
            log_callback=log_callback,
        )

    prices = prices_by_code(price_items)
    validate_availability_coverage(
        product_codes,
        availability,
        log_callback=log_callback,
    )
    rows, skipped = build_rows(products, availability, prices)
    if not rows:
        raise RuntimeError("Motonet catalog contains no available priced products.")

    destination = Path(output_path)
    await asyncio.to_thread(save_excel, rows, destination)
    log(
        f"Motonet Excel created: path={destination}, products={len(rows)}, skipped={skipped}",
        log_callback,
    )
