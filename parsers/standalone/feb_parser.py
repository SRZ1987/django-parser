from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

from .public_commerce_parser import (
    COLUMNS,
    MAX_RETRIES,
    RETRYABLE_STATUSES,
    RETRY_BASE_DELAY,
    WORKSHEET_NAME,
    clean_text,
    extract_barcode,
    log,
    parse_decimal_money,
    retry_after_seconds,
    save_excel,
)
from .sitemap_retailers_parser import (
    ProductMarkupMissing,
    ProductPageMissing,
    extract_jsonld,
    first_offer,
    parse_sitemap_products,
    product_image,
    redirected_to_store_home,
    stable_hash,
)


STORE_NAME = "FEB"
STORE_CODE = "feb"
BASE_URL = "https://www.feb.ee/"
SITEMAP_URL = "https://www.feb.ee/sitemap_et.xml"
REQUEST_TIMEOUT = 45
PRODUCT_MARKUP_RETRIES = 3
MAX_MISSING_RATIO = 0.02
MIN_SITEMAP_PRODUCTS = 10000


def _bounded_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, 1), maximum)


def _bounded_float(name: str, default: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, 0.5), maximum)


# Environment overrides are deliberately capped to prevent an accidental request storm.
CONCURRENCY = _bounded_int("FEB_CONCURRENCY", 10, 10)
REQUESTS_PER_SECOND = _bounded_float("FEB_REQUESTS_PER_SECOND", 4.0, 8.0)


class CatalogIncomplete(RuntimeError):
    pass


@dataclass
class FetchStats:
    processed: int = 0
    exported: int = 0
    unavailable: int = 0
    missing: int = 0


class RequestPacer:
    def __init__(self, requests_per_second: float, *, clock=time.monotonic):
        self._interval = 1.0 / requests_per_second
        self._next_start = 0.0
        self._lock = asyncio.Lock()
        self._clock = clock

    async def wait(self) -> None:
        async with self._lock:
            now = self._clock()
            delay = max(self._next_start - now, 0.0)
            self._next_start = max(self._next_start, now) + self._interval
        if delay:
            await asyncio.sleep(delay)


async def request_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
    label: str,
    pacer: RequestPacer,
    log_callback=None,
) -> str:
    for attempt in range(1, MAX_RETRIES + 1):
        await pacer.wait()
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    if redirected_to_store_home(url, str(response.url), BASE_URL):
                        raise ProductPageMissing(url)
                    return await response.text()
                if response.status == 404:
                    raise ProductPageMissing(url)

                body = clean_text((await response.text())[:300])
                if response.status not in RETRYABLE_STATUSES or attempt >= MAX_RETRIES:
                    raise RuntimeError(
                        f"{STORE_NAME} {label} failed with HTTP {response.status}: {body}"
                    )

                delay = retry_after_seconds(response.headers.get("Retry-After"))
                if delay is None:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
                log(
                    f"{STORE_NAME} HTTP {response.status}: {label}, "
                    f"retry={attempt}/{MAX_RETRIES}, delay={delay:.1f}s",
                    log_callback,
                )
                await asyncio.sleep(delay)
        except ProductPageMissing:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    f"{STORE_NAME} {label} failed after retries: {exc}"
                ) from exc
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
            log(
                f"{STORE_NAME} request error: {label}, retry={attempt}/{MAX_RETRIES}, "
                f"delay={delay:.1f}s, error={exc}",
                log_callback,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"{STORE_NAME} {label} exhausted retries.")


def _schema_name(value: Any) -> str:
    if isinstance(value, dict):
        return clean_text(value.get("name"))
    return clean_text(value)


def _breadcrumb_category(
    breadcrumbs: list[dict[str, Any]],
    product_name: str,
) -> tuple[str, str]:
    # The final breadcrumb is the product page itself; the preceding item is its category.
    category_items = breadcrumbs[:-1] if len(breadcrumbs) > 1 else breadcrumbs
    for list_item in reversed(category_items):
        item = list_item.get("item") or {}
        if isinstance(item, dict):
            name = clean_text(item.get("name"))
            url = clean_text(item.get("@id"))
        else:
            name = clean_text(list_item.get("name"))
            url = clean_text(item)
        if name and name.casefold() != product_name.casefold():
            return name, url
    return "", ""


def parse_product_page(
    html_text: str,
    requested_url: str,
    fallback_image: str = "",
) -> list[Any] | None:
    soup = BeautifulSoup(html_text, "html.parser")
    product, breadcrumbs = extract_jsonld(soup)
    if not product:
        raise ProductMarkupMissing(f"FEB product JSON-LD is missing: {requested_url}")

    offer = first_offer(product)
    availability = clean_text(offer.get("availability")).casefold().rstrip("/")
    if not availability:
        raise ProductMarkupMissing(f"FEB product availability is missing: {requested_url}")
    if not availability.endswith("instock"):
        return None

    name = clean_text(product.get("name"))
    product_id = clean_text(product.get("productID") or product.get("sku") or offer.get("sku"))
    current_price = parse_decimal_money(offer.get("price"))
    if not name or not product_id or current_price == "":
        raise ProductMarkupMissing(
            f"FEB required product data is missing (name, productID or price): {requested_url}"
        )

    old_price_node = soup.select_one(
        ".old-price [data-price-amount], [data-price-type='oldPrice'][data-price-amount]"
    )
    old_price = (
        parse_decimal_money(old_price_node.get("data-price-amount"))
        if old_price_node is not None
        else ""
    )
    if old_price != "" and old_price > current_price:
        price, sale_price = old_price, current_price
    else:
        price, sale_price = current_price, ""

    category_name = clean_text(product.get("category"))
    category_url = ""
    if not category_name:
        category_name, category_url = _breadcrumb_category(breadcrumbs, name)

    canonical_url = urljoin(BASE_URL, clean_text(offer.get("url")) or requested_url)
    sku = clean_text(product.get("sku") or offer.get("sku") or product_id)
    category_id = (
        f"feb-category-{stable_hash(category_url or category_name.casefold())}"
        if category_name
        else ""
    )

    return [
        name,
        price,
        sale_price,
        "",
        extract_barcode(product),
        f"feb-{product_id}",
        product_image(product, fallback_image),
        canonical_url,
        sku,
        category_name,
        category_id,
        clean_text(product.get("description")),
        _schema_name(product.get("brand")),
        clean_text(product.get("model") or product.get("mpn")),
    ]


def validate_catalog_snapshot(
    *,
    sitemap_products: int,
    exported_products: int,
    missing_products: int,
) -> None:
    if sitemap_products < MIN_SITEMAP_PRODUCTS:
        raise CatalogIncomplete(
            f"FEB sitemap is anomalously small: {sitemap_products} product cards; "
            f"expected at least {MIN_SITEMAP_PRODUCTS}."
        )
    if exported_products <= 0:
        raise CatalogIncomplete("FEB catalog contains no available products.")
    missing_ratio = missing_products / sitemap_products
    if missing_ratio > MAX_MISSING_RATIO:
        raise CatalogIncomplete(
            f"FEB catalog is incomplete: missing={missing_products}/{sitemap_products} "
            f"({missing_ratio:.1%}), allowed={MAX_MISSING_RATIO:.1%}."
        )


async def fetch_rows(
    session: aiohttp.ClientSession,
    products: list[tuple[str, str]],
    log_callback=None,
    *,
    concurrency: int = CONCURRENCY,
    pacer: RequestPacer | None = None,
) -> tuple[list[list[Any]], FetchStats]:
    pacer = pacer or RequestPacer(REQUESTS_PER_SECOND)
    queue: asyncio.Queue[tuple[int, tuple[str, str]]] = asyncio.Queue()
    for index, product in enumerate(products, start=1):
        queue.put_nowait((index, product))

    stats = FetchStats()
    rows_by_external_id: dict[str, list[Any]] = {}
    errors: list[str] = []

    async def process_product(index: int, product: tuple[str, str]) -> None:
        product_url, fallback_image = product
        for markup_attempt in range(1, PRODUCT_MARKUP_RETRIES + 1):
            html_text = await request_text(
                session,
                product_url,
                label=f"product {index}/{len(products)}",
                pacer=pacer,
                log_callback=log_callback,
            )
            try:
                row = parse_product_page(html_text, product_url, fallback_image)
                break
            except ProductMarkupMissing as exc:
                if markup_attempt >= PRODUCT_MARKUP_RETRIES:
                    raise
                delay = RETRY_BASE_DELAY * (2 ** (markup_attempt - 1))
                log(
                    f"WARNING: {exc}; retry={markup_attempt}/{PRODUCT_MARKUP_RETRIES}, "
                    f"delay={delay:.1f}s",
                    log_callback,
                )
                await asyncio.sleep(delay)

        if row is None:
            stats.unavailable += 1
            return

        external_id = clean_text(row[5])
        previous = rows_by_external_id.get(external_id)
        if previous is not None and clean_text(previous[7]) != clean_text(row[7]):
            raise CatalogIncomplete(
                f"FEB duplicate external_id {external_id} is used by different product URLs: "
                f"{previous[7]} and {row[7]}."
            )
        rows_by_external_id[external_id] = row

    async def worker() -> None:
        while True:
            index, product = await queue.get()
            try:
                await process_product(index, product)
            except ProductPageMissing:
                stats.missing += 1
                log(
                    f"WARNING: FEB product was removed or redirected to the store home: "
                    f"{product[0]}",
                    log_callback,
                )
            except Exception as exc:
                message = f"FEB product failed: url={product[0]}, error={exc}"
                errors.append(message)
                log(f"ERROR: {message}", log_callback)
            finally:
                stats.processed += 1
                if stats.processed == len(products) or stats.processed % 100 == 0:
                    log(
                        f"FEB download progress: cards={stats.processed}/{len(products)}, "
                        f"products={len(rows_by_external_id)}, unavailable={stats.unavailable}, "
                        f"missing={stats.missing}, queue={queue.qsize()}",
                        log_callback,
                    )
                queue.task_done()

    worker_count = min(max(concurrency, 1), max(len(products), 1))
    workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
    join_task = asyncio.create_task(queue.join())
    try:
        done, _pending = await asyncio.wait(
            [join_task, *workers],
            return_when=asyncio.FIRST_COMPLETED,
        )
        stopped_workers = [task for task in workers if task in done]
        if stopped_workers:
            exception = stopped_workers[0].exception()
            raise CatalogIncomplete(
                f"FEB download worker stopped unexpectedly: {exception or 'no error details'}"
            )
        await join_task
    finally:
        if not join_task.done():
            join_task.cancel()
        for task in workers:
            task.cancel()
        await asyncio.gather(join_task, *workers, return_exceptions=True)

    if errors:
        preview = "; ".join(errors[:5])
        raise CatalogIncomplete(
            f"FEB catalog download is incomplete: failed_pages={len(errors)}. {preview}"
        )

    rows = sorted(
        rows_by_external_id.values(),
        key=lambda row: (clean_text(row[0]).casefold(), clean_text(row[5])),
    )
    stats.exported = len(rows)
    validate_catalog_snapshot(
        sitemap_products=len(products),
        exported_products=len(rows),
        missing_products=stats.missing,
    )
    return rows, stats


async def main(output_path: str | Path, log_callback=None) -> None:
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, connect=15)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "et-EE,et;q=0.9,en;q=0.7",
        "User-Agent": "PriceCompareCatalogBot/1.0 (+https://tannenberg.up.railway.app/)",
    }
    pacer = RequestPacer(REQUESTS_PER_SECOND)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
        sitemap_xml = await request_text(
            session,
            SITEMAP_URL,
            label="product sitemap",
            pacer=pacer,
            log_callback=log_callback,
        )
        products = parse_sitemap_products(sitemap_xml)
        if len(products) < MIN_SITEMAP_PRODUCTS:
            raise CatalogIncomplete(
                f"FEB sitemap is anomalously small: {len(products)} product cards; "
                f"expected at least {MIN_SITEMAP_PRODUCTS}."
            )
        log(
            f"FEB sitemap: product_cards={len(products)}, workers={CONCURRENCY}, "
            f"request_rate={REQUESTS_PER_SECOND:.1f}/s",
            log_callback,
        )
        rows, stats = await fetch_rows(session, products, log_callback, pacer=pacer)

    destination = Path(output_path)
    await asyncio.to_thread(save_excel, rows, destination)
    log(
        f"FEB Excel created: path={destination}, products={stats.exported}, "
        f"unavailable={stats.unavailable}, missing={stats.missing}",
        log_callback,
    )
