from __future__ import annotations

import asyncio
import html
import random
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import aiohttp
from openpyxl import Workbook


BASE_URL = "https://handymann.ee/"
PRODUCTS_API_URL = f"{BASE_URL}wp-json/wc/store/v1/products"
OUTPUT_FILE = Path("handymann.xlsx")
WORKSHEET_NAME = "Товары"

PAGE_SIZE = 100
CONCURRENCY = 5
REQUEST_TIMEOUT = 45
MAX_RETRIES = 5
RETRY_BASE_DELAY = 1.0

COLUMNS = [
    "Название товара",
    "Цена",
    "Цена со скидкой",
    "Цена со скидкой 2",
    "Штрихкод",
    "Код магазина",
    "Фото",
    "Ссылка",
]

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


def parse_money(value: Any, minor_unit: int) -> float | str:
    if value in (None, "") or isinstance(value, bool):
        return ""
    try:
        return round(int(str(value)) / (10 ** minor_unit), minor_unit)
    except (TypeError, ValueError, OverflowError):
        return ""


def extract_prices(product: dict[str, Any]) -> tuple[float | str, float | str]:
    prices = product.get("prices") or {}
    try:
        minor_unit = min(max(int(prices.get("currency_minor_unit", 2)), 0), 6)
    except (TypeError, ValueError):
        minor_unit = 2

    current = parse_money(prices.get("price"), minor_unit)
    regular = parse_money(prices.get("regular_price"), minor_unit)
    sale = parse_money(prices.get("sale_price"), minor_unit)

    if regular != "" and current != "" and current < regular:
        return regular, current
    if regular != "" and sale != "" and sale < regular:
        return regular, sale
    return current if current != "" else regular, ""


def normalize_product(product: dict[str, Any]) -> list[Any] | None:
    if not product.get("is_in_stock", False):
        return None

    name = clean_text(product.get("name"))
    sku = clean_text(product.get("sku"))
    product_id = clean_text(product.get("id"))
    external_id = sku or (f"wc-{product_id}" if product_id else "")
    product_url = clean_text(product.get("permalink"))
    if not name or not external_id or not product_url:
        return None

    images = product.get("images") or []
    image_url = clean_text(images[0].get("src")) if images and isinstance(images[0], dict) else ""
    price, sale_price = extract_prices(product)
    return [
        name,
        price,
        sale_price,
        "",
        extract_barcode(product),
        external_id,
        image_url,
        product_url,
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


async def request_page(session: aiohttp.ClientSession, page: int, log_callback=None):
    params = {
        "page": page,
        "per_page": PAGE_SIZE,
        "orderby": "id",
        "order": "asc",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(PRODUCTS_API_URL, params=params) as response:
                status = response.status
                if status == 200:
                    payload = await response.json(content_type=None)
                    if not isinstance(payload, list):
                        raise RuntimeError(f"Handymann API page {page} returned non-list JSON.")
                    return payload, dict(response.headers)

                body = (await response.text())[:300]
                if status not in RETRYABLE_STATUSES or attempt >= MAX_RETRIES:
                    raise RuntimeError(f"Handymann API page {page} failed with HTTP {status}: {body}")

                delay = retry_after_seconds(response.headers.get("Retry-After"))
                if delay is None:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
                log(
                    f"Handymann API HTTP {status}: page={page}, retry={attempt}/{MAX_RETRIES}, delay={delay:.1f}s",
                    log_callback,
                )
                await asyncio.sleep(delay)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt >= MAX_RETRIES:
                raise RuntimeError(f"Handymann API page {page} failed after retries: {exc}") from exc
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
            log(
                f"Handymann API request error: page={page}, retry={attempt}/{MAX_RETRIES}, "
                f"delay={delay:.1f}s, error={exc}",
                log_callback,
            )
            await asyncio.sleep(delay)

    raise RuntimeError(f"Handymann API page {page} exhausted retries.")


async def fetch_catalog(log_callback=None) -> list[dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, connect=15)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY)
    headers = {
        "Accept": "application/json",
        "Accept-Language": "et-EE,et;q=0.9,en;q=0.7",
        "User-Agent": "PriceCompareCatalogBot/1.0 (+https://handymann.ee/)",
    }

    async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
        first_page, response_headers = await request_page(session, 1, log_callback)
        try:
            total = int(response_headers["X-WP-Total"])
            total_pages = int(response_headers["X-WP-TotalPages"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Handymann API did not return catalog pagination headers.") from exc

        if total <= 0 or total_pages <= 0 or not first_page:
            raise RuntimeError("Handymann API returned an empty catalog.")

        log(
            f"Handymann API: endpoint={PRODUCTS_API_URL}, status=200, products={total}, pages={total_pages}",
            log_callback,
        )
        pages: dict[int, list[dict[str, Any]]] = {1: first_page}
        completed = 1
        progress_lock = asyncio.Lock()
        limiter = asyncio.Semaphore(CONCURRENCY)

        async def load_page(page: int) -> None:
            nonlocal completed
            async with limiter:
                records, _headers = await request_page(session, page, log_callback)
            pages[page] = records
            async with progress_lock:
                completed += 1
                if completed == total_pages or completed % 5 == 0:
                    downloaded = sum(len(items) for items in pages.values())
                    log(
                        f"Handymann download progress: pages={completed}/{total_pages}, products={downloaded}/{total}",
                        log_callback,
                    )

        await asyncio.gather(*(load_page(page) for page in range(2, total_pages + 1)))
        products = [product for page in range(1, total_pages + 1) for product in pages[page]]

    product_ids = {str(product.get("id")) for product in products if product.get("id") not in (None, "")}
    if len(product_ids) < total:
        raise RuntimeError(
            f"Handymann catalog is incomplete: expected={total}, unique_products={len(product_ids)}."
        )
    return products


def save_excel(rows: list[list[Any]], output_path: Path) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = WORKSHEET_NAME
    worksheet.append(COLUMNS)
    for row in rows:
        worksheet.append(row)

    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    widths = {"A": 65, "B": 14, "C": 18, "D": 20, "E": 22, "F": 22, "G": 70, "H": 80}
    for column, width in widths.items():
        worksheet.column_dimensions[column].width = width
    for row_number in range(2, worksheet.max_row + 1):
        worksheet[f"E{row_number}"].number_format = "@"
        worksheet[f"F{row_number}"].number_format = "@"
        for column in ("B", "C", "D"):
            worksheet[f"{column}{row_number}"].number_format = "0.00"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)
    workbook.close()


async def main(output_path: str | Path | None = None, log_callback=None) -> None:
    destination = Path(output_path) if output_path is not None else OUTPUT_FILE
    products = await fetch_catalog(log_callback)

    rows_by_external_id: dict[str, list[Any]] = {}
    skipped = 0
    for product in products:
        row = normalize_product(product)
        if row is None:
            skipped += 1
            continue
        external_id = row[5]
        existing = rows_by_external_id.get(external_id)
        if existing is not None and existing[7] != row[7]:
            raise RuntimeError(
                f"Handymann catalog contains duplicate SKU/external_id {external_id!r} for different product URLs."
            )
        rows_by_external_id[external_id] = row

    rows = sorted(rows_by_external_id.values(), key=lambda item: (str(item[0]).casefold(), str(item[5])))
    if not rows:
        raise RuntimeError("Handymann catalog has no available products to export.")

    await asyncio.to_thread(save_excel, rows, destination)
    log(
        f"Handymann Excel created: path={destination}, products={len(rows)}, skipped={skipped}",
        log_callback,
    )


if __name__ == "__main__":
    asyncio.run(main())
