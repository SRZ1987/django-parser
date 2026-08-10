from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import aiohttp
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession as CurlAsyncSession

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
from .sitemap_retailers_parser import stable_hash


CONCURRENCY = 4
REQUEST_TIMEOUT = 90
HAMMERJACK_PAGE_SIZE = 100
ELEKTRIKAUP_PAGE_SIZE = 50000
MIN_EXPORTED_PRODUCTS = {"hammerjack": 5000, "elektrikaup": 500}


@dataclass(frozen=True)
class ListingRetailer:
    code: str
    name: str
    base_url: str
    catalog_url: str


LISTING_RETAILERS = {
    store.code: store
    for store in (
        ListingRetailer(
            code="hammerjack",
            name="Hammerjack",
            base_url="https://hammerjack.eu/et",
            catalog_url="https://hammerjack.eu/et",
        ),
        ListingRetailer(
            code="elektrikaup",
            name="Elektrikaup",
            base_url="https://www.elektrikaup.ee/",
            catalog_url=(
                "https://www.elektrikaup.ee/tooted?type_id=1&"
                f"rows_per_page={ELEKTRIKAUP_PAGE_SIZE}&page=0"
            ),
        ),
    )
}


def _with_query(url: str, **values: Any) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in values.items()})
    return urlunparse(parsed._replace(query=urlencode(query)))


async def request_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
    store: ListingRetailer,
    label: str,
    log_callback=None,
) -> str:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    return await response.text()
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


async def request_curl_text(
    session: CurlAsyncSession,
    url: str,
    *,
    store: ListingRetailer,
    label: str,
    log_callback=None,
) -> str:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await session.get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                return response.text
            body = response.text[:300]
            if response.status_code not in RETRYABLE_STATUSES or attempt >= MAX_RETRIES:
                raise RuntimeError(
                    f"{store.name} {label} failed with HTTP {response.status_code}: {body}"
                )
            delay = retry_after_seconds(response.headers.get("Retry-After"))
            if delay is None:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
            log(
                f"{store.name} HTTP {response.status_code}: {label}, "
                f"retry={attempt}/{MAX_RETRIES}, delay={delay:.1f}s",
                log_callback,
            )
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except RuntimeError:
            raise
        except Exception as exc:
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


def _price_from_text(value: Any) -> float | str:
    text = clean_text(value)
    match = re.search(r"\d[\d\s]*(?:[,.]\d+)?", text)
    if not match:
        return ""
    return parse_decimal_money(match.group(0).replace(" ", "").replace(",", "."))


def discover_hammerjack_categories(html_text: str, base_url: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    categories = []
    for anchor in soup.select(".header-menu .picture-title-wrap .title a[href]"):
        name = clean_text(anchor.get_text(" "))
        url = urljoin(base_url, clean_text(anchor.get("href")))
        if name and url:
            categories.append((name, url))
    return list(dict.fromkeys(categories))


def hammerjack_page_count(html_text: str) -> int:
    soup = BeautifulSoup(html_text, "html.parser")
    pages = [1]
    for anchor in soup.select(".pager a[href]"):
        match = re.search(r"(?:\?|&)pagenumber=(\d+)", clean_text(anchor.get("href")))
        if match:
            pages.append(int(match.group(1)))
    return max(pages)


def parse_hammerjack_page(
    html_text: str,
    *,
    category_name: str,
    category_url: str,
    base_url: str,
) -> list[list[Any]]:
    soup = BeautifulSoup(html_text, "html.parser")
    rows = []
    for item in soup.select(".product-item[data-productid]"):
        product_id = clean_text(item.get("data-productid"))
        name_anchor = item.select_one(".product-title a[href]")
        name = clean_text(name_anchor.get_text(" ")) if name_anchor else ""
        sku_node = item.select_one(".sku")
        sku = clean_text(sku_node.get_text(" ")) if sku_node else ""
        actual_node = item.select_one(".prices .actual-price, .prices .price")
        old_node = item.select_one(".prices .old-price")
        actual = _price_from_text(actual_node.get_text(" ")) if actual_node else ""
        old = _price_from_text(old_node.get_text(" ")) if old_node else ""
        if old != "" and actual != "" and actual < old:
            price, sale_price = old, actual
        else:
            price, sale_price = actual, ""
        stock_node = item.select_one(".stock-overview")
        stock_text = clean_text(stock_node.get_text(" ")).casefold() if stock_node else ""
        unavailable = any(
            marker in stock_text
            for marker in ("laost otsas", "pole saadaval", "ei ole saadaval", "out of stock")
        )
        product_path = clean_text(name_anchor.get("href")) if name_anchor else ""
        if not product_id or not name or not product_path or price == "" or unavailable:
            continue
        image = item.select_one(".picture img")
        description = item.select_one(".description")
        rows.append([
            name,
            price,
            sale_price,
            "",
            "",
            f"hammerjack-{product_id}",
            clean_text(image.get("src") or image.get("data-src")) if image else "",
            urljoin(base_url, product_path),
            sku or product_id,
            category_name,
            f"hammerjack-category-{stable_hash(category_url)}",
            clean_text(description.get_text(" ")) if description else "",
        ])
    return rows


async def fetch_hammerjack_rows(
    session: CurlAsyncSession,
    store: ListingRetailer,
    log_callback=None,
) -> list[list[Any]]:
    home_html = await request_curl_text(
        session,
        store.catalog_url,
        store=store,
        label="catalog navigation",
        log_callback=log_callback,
    )
    categories = discover_hammerjack_categories(home_html, store.base_url)
    if not categories:
        raise RuntimeError("Hammerjack catalog navigation contains no root categories.")

    limiter = asyncio.Semaphore(CONCURRENCY)
    page_jobs: list[tuple[str, str, int, str | None]] = []

    async def first_page(category_name: str, category_url: str):
        async with limiter:
            html_text = await request_curl_text(
                session,
                _with_query(category_url, pagesize=HAMMERJACK_PAGE_SIZE, pagenumber=1),
                store=store,
                label=f"category {category_name} page 1",
                log_callback=log_callback,
            )
        return category_name, category_url, html_text, hammerjack_page_count(html_text)

    first_pages = await asyncio.gather(*(first_page(*category) for category in categories))
    for category_name, category_url, html_text, page_count in first_pages:
        page_jobs.append((category_name, category_url, 1, html_text))
        page_jobs.extend(
            (category_name, category_url, page, None) for page in range(2, page_count + 1)
        )
    log(
        f"Hammerjack catalog: categories={len(categories)}, pages={len(page_jobs)}",
        log_callback,
    )

    completed = 0
    progress_lock = asyncio.Lock()

    async def fetch_page(job: tuple[str, str, int, str | None]):
        nonlocal completed
        category_name, category_url, page, cached_html = job
        if cached_html is None:
            async with limiter:
                cached_html = await request_curl_text(
                    session,
                    _with_query(
                        category_url,
                        pagesize=HAMMERJACK_PAGE_SIZE,
                        pagenumber=page,
                    ),
                    store=store,
                    label=f"category {category_name} page {page}",
                    log_callback=log_callback,
                )
        rows = parse_hammerjack_page(
            cached_html,
            category_name=category_name,
            category_url=category_url,
            base_url=store.base_url,
        )
        async with progress_lock:
            completed += 1
            if completed == len(page_jobs) or completed % 20 == 0:
                log(
                    f"Hammerjack download progress: pages={completed}/{len(page_jobs)}",
                    log_callback,
                )
        return rows

    page_rows = await asyncio.gather(*(fetch_page(job) for job in page_jobs))
    return [row for rows in page_rows for row in rows]


def parse_elektrikaup_catalog(html_text: str, base_url: str) -> list[list[Any]]:
    soup = BeautifulSoup(html_text, "html.parser")
    rows = []
    for item in soup.select(".product"):
        name_anchor = item.select_one(".nameBlock a.name[href]")
        name = clean_text(name_anchor.get_text(" ")) if name_anchor else ""
        product_path = clean_text(name_anchor.get("href")) if name_anchor else ""
        product_id_match = re.search(r"/(\d+)(?:/|$)", urlparse(product_path).path)
        price_node = item.select_one(".price.myyn, .price")
        price = _price_from_text(price_node.get_text(" ")) if price_node else ""
        quantity_node = item.select_one(".quantity")
        quantity_text = clean_text(quantity_node.get_text(" ")).casefold() if quantity_node else ""
        unavailable = "ei ole" in quantity_text or bool(re.search(r"\b0\s+\S+\s*$", quantity_text))
        if not name or not product_path or not product_id_match or price == "" or unavailable:
            continue

        product_id = product_id_match.group(1)
        image = item.select_one(".photo img")
        description = item.select_one(".description")
        path_parts = [part for part in urlparse(product_path).path.split("/") if part]
        category_slug = path_parts[0] if path_parts else ""
        category_name = category_slug.replace("-", " ").strip().title()
        rows.append([
            name,
            price,
            "",
            "",
            "",
            f"elektrikaup-{product_id}",
            clean_text(image.get("src")) if image else "",
            urljoin(base_url, product_path),
            product_id,
            category_name,
            f"elektrikaup-category-{category_slug}" if category_slug else "",
            clean_text(description.get_text(" ")) if description else "",
        ])
    return rows


async def fetch_elektrikaup_rows(
    session: aiohttp.ClientSession,
    store: ListingRetailer,
    log_callback=None,
) -> list[list[Any]]:
    html_text = await request_text(
        session,
        store.catalog_url,
        store=store,
        label="complete product listing",
        log_callback=log_callback,
    )
    rows = parse_elektrikaup_catalog(html_text, store.base_url)
    log(f"Elektrikaup complete listing: products={len(rows)}", log_callback)
    return rows


def deduplicate_rows(store: ListingRetailer, rows: list[list[Any]]) -> list[list[Any]]:
    rows_by_id: dict[str, list[Any]] = {}
    for row in rows:
        external_id = row[5]
        existing = rows_by_id.get(external_id)
        if existing is not None and existing[7] != row[7]:
            raise RuntimeError(
                f"{store.name} duplicate external_id {external_id!r} has different URLs."
            )
        rows_by_id[external_id] = row
    result = sorted(rows_by_id.values(), key=lambda row: (str(row[0]).casefold(), str(row[5])))
    minimum = MIN_EXPORTED_PRODUCTS[store.code]
    if len(result) < minimum:
        raise RuntimeError(
            f"{store.name} catalog is unexpectedly small: products={len(result)}, "
            f"minimum={minimum}."
        )
    return result


async def main(store_code: str, output_path: str | Path, log_callback=None) -> None:
    try:
        store = LISTING_RETAILERS[store_code]
    except KeyError as exc:
        raise ValueError(f"Unknown listing retailer: {store_code}") from exc

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "et-EE,et;q=0.9,en;q=0.7",
        "User-Agent": "PriceCompareCatalogBot/1.0",
    }
    if store.code == "hammerjack":
        async with CurlAsyncSession(
            headers=headers,
            impersonate="chrome",
            max_clients=CONCURRENCY,
        ) as session:
            raw_rows = await fetch_hammerjack_rows(session, store, log_callback)
    else:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, connect=20)
        connector = aiohttp.TCPConnector(limit=CONCURRENCY)
        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers=headers,
        ) as session:
            raw_rows = await fetch_elektrikaup_rows(session, store, log_callback)
    rows = deduplicate_rows(store, raw_rows)

    destination = Path(output_path)
    await asyncio.to_thread(save_excel, rows, destination)
    log(
        f"{store.name} Excel created: path={destination}, products={len(rows)}",
        log_callback,
    )
