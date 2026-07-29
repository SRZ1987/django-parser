from __future__ import annotations

import asyncio
import hashlib
import html
import math
import random
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import aiohttp


API_PRODUCTS_URL = "https://espak.ee/epood/wp-json/wc/store/v1/products"
API_CATEGORIES_URL = "https://espak.ee/epood/wp-json/wc/store/v1/products/categories"
ESPAK_WEBSITE_URL = "https://espak.ee/epood/"

PER_PAGE = 100
CONCURRENCY = 8
REQUEST_TIMEOUT = 45
MAX_RETRIES = 6
RETRY_BASE_DELAY = 2.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "et-EE,et;q=0.9,en;q=0.8,ru;q=0.7",
    "Referer": ESPAK_WEBSITE_URL,
}

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
PRICE_RE = re.compile(r"-?\d+(?:[,.]\d+)?")


def clean_text(value: Any) -> str:
    if value in (None, ""):
        return ""

    text = html.unescape(str(value))
    text = TAG_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def stable_external_id(*values: Any) -> str:
    source = "|".join(clean_text(value) for value in values if clean_text(value))
    if not source:
        source = "unknown"
    return hashlib.sha1(source.encode("utf-8")).hexdigest()


def parse_price(value: Any, minor_unit: int | None = None) -> Decimal | None:
    if value in (None, ""):
        return None

    text = clean_text(value).replace("\xa0", " ").strip()
    if not text:
        return None

    if minor_unit is not None and text.isdigit():
        try:
            return (Decimal(text) / (Decimal(10) ** int(minor_unit))).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            return None

    match = PRICE_RE.search(text)
    if not match:
        return None

    normalized = match.group(0).replace(",", ".")
    try:
        return Decimal(normalized).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def get_attribute(product: dict[str, Any], *names: str) -> str:
    wanted = {name.casefold().strip() for name in names}

    for attribute in product.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue

        attribute_name = clean_text(attribute.get("name")).casefold()
        if attribute_name not in wanted:
            continue

        values = [
            clean_text(term.get("name"))
            for term in attribute.get("terms") or []
            if isinstance(term, dict) and clean_text(term.get("name"))
        ]
        if values:
            return " | ".join(values)

    return ""


class EspakClient:
    def __init__(self, log_callback: Callable[[str], None] | None = None):
        self.log_callback = log_callback
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(CONCURRENCY)

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def open(self):
        if self._session is not None:
            return

        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        connector = aiohttp.TCPConnector(
            limit=CONCURRENCY,
            limit_per_host=CONCURRENCY,
            ttl_dns_cache=300,
        )
        self._session = aiohttp.ClientSession(
            headers=HEADERS,
            timeout=timeout,
            connector=connector,
        )

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch_categories(self) -> list[dict[str, Any]]:
        return await self._collect_all(API_CATEGORIES_URL, "categories", orderby="name")

    async def fetch_products(self) -> list[dict[str, Any]]:
        return await self._collect_all(API_PRODUCTS_URL, "products", orderby="id")

    async def fetch_product_details(self, product: dict[str, Any]) -> dict[str, Any]:
        return product

    async def _collect_all(self, url: str, resource_name: str, orderby: str) -> list[dict[str, Any]]:
        first_page, headers = await self._request_json(
            url,
            {"page": 1, "per_page": PER_PAGE, "orderby": orderby, "order": "asc"},
        )

        total_pages = self._get_total_pages(headers)
        if total_pages is None:
            self._log(f"ESPAK API did not return total pages for {resource_name}; loading until empty page.")
            return await self._collect_until_empty(url, first_page, orderby)

        self._log(f"ESPAK {resource_name}: pages={total_pages}, first_page_items={len(first_page)}")
        if total_pages <= 1:
            return first_page

        all_items = list(first_page)
        tasks = [
            asyncio.create_task(self._fetch_page(url, page, orderby))
            for page in range(2, total_pages + 1)
        ]

        completed = 1
        for task in asyncio.as_completed(tasks):
            page, items = await task
            completed += 1
            all_items.extend(items)
            if completed == total_pages or completed % 10 == 0:
                self._log(
                    f"ESPAK {resource_name}: pages {completed}/{total_pages}, items={len(all_items)}"
                )
            if not items:
                self._log(f"ESPAK {resource_name}: page {page} is empty.")

        return all_items

    async def _collect_until_empty(
        self,
        url: str,
        first_page: list[dict[str, Any]],
        orderby: str,
    ) -> list[dict[str, Any]]:
        all_items = list(first_page)
        next_page = 2

        while True:
            page_numbers = list(range(next_page, next_page + CONCURRENCY))
            results = await asyncio.gather(*[self._fetch_page(url, page, orderby) for page in page_numbers])
            results.sort(key=lambda item: item[0])

            found_empty = False
            for page, items in results:
                if not items:
                    found_empty = True
                    break
                all_items.extend(items)
                self._log(f"ESPAK page {page} loaded, items={len(all_items)}")

            if found_empty:
                break

            next_page += CONCURRENCY

        return all_items

    async def _fetch_page(self, url: str, page: int, orderby: str) -> tuple[int, list[dict[str, Any]]]:
        async with self._semaphore:
            items, _ = await self._request_json(
                url,
                {"page": page, "per_page": PER_PAGE, "orderby": orderby, "order": "asc"},
            )
        return page, items

    async def _request_json(
        self,
        url: str,
        params: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], aiohttp.typedefs.LooseHeaders]:
        await self.open()
        if self._session is None:
            raise RuntimeError("ESPAK HTTP session is not initialized.")

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with self._session.get(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json(content_type=None)
                        if not isinstance(data, list):
                            raise RuntimeError(f"ESPAK API returned {type(data).__name__}, expected list.")
                        return data, response.headers

                    if response.status in {429, 500, 502, 503, 504}:
                        await self._sleep_before_retry(response, attempt)
                        continue

                    body = await response.text()
                    raise RuntimeError(f"ESPAK HTTP {response.status}: {body[:300]}")

            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as error:
                if attempt == MAX_RETRIES:
                    raise RuntimeError(
                        f"ESPAK request failed after {MAX_RETRIES} attempts: {error}"
                    ) from error

                delay = self._retry_delay(attempt)
                self._log(f"ESPAK request error: {error}. Retry {attempt}/{MAX_RETRIES} in {delay:.1f}s.")
                await asyncio.sleep(delay)

        raise RuntimeError("ESPAK request failed.")

    async def _sleep_before_retry(self, response: aiohttp.ClientResponse, attempt: int):
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else 0
        except (TypeError, ValueError):
            delay = 0

        if delay <= 0:
            delay = self._retry_delay(attempt)

        self._log(f"ESPAK HTTP {response.status}. Retry {attempt}/{MAX_RETRIES} in {delay:.1f}s.")
        await asyncio.sleep(delay)

    def _get_total_pages(self, headers: aiohttp.typedefs.LooseHeaders) -> int | None:
        total_pages_raw = headers.get("X-WP-TotalPages")
        total_items_raw = headers.get("X-WP-Total")

        if total_pages_raw and str(total_pages_raw).isdigit():
            return int(total_pages_raw)

        if total_items_raw and str(total_items_raw).isdigit():
            return math.ceil(int(total_items_raw) / PER_PAGE)

        return None

    def _retry_delay(self, attempt: int) -> float:
        return RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0.2, 1.2)

    def _log(self, message: str):
        if self.log_callback:
            self.log_callback(message)
