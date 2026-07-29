from __future__ import annotations

import asyncio
import random
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import aiohttp


GRAPHQL_URL = "https://online.depo.ee/graphql"
DEPO_WEBSITE_URL = "https://online.depo.ee"

ROWS = 20
WORKERS = 3
REQUEST_DELAY = 0.1
RETRY_WAIT = 20
MAX_RETRIES = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": DEPO_WEBSITE_URL,
    "Referer": f"{DEPO_WEBSITE_URL}/",
    "Accept-Language": "ru-RU,ru;q=0.9,et-EE;q=0.8,et;q=0.7,en-US;q=0.6,en;q=0.5",
}

COOKIES = {"Depo.Language": "ee"}

MAIN_CATEGORIES_QUERY = """
query categoriesHomepage {
  categories(where: {showOnHomepage: {eq: true}}) {
    nodes { id }
  }
}
"""

PRODUCTS_QUERY = """
query products($categoryId: Int, $rows: Int, $start: Int) {
  products(categoryId: $categoryId, rows: $rows, start: $start, facets: []) {
    pageInfo { totalCount }
    edges {
      node {
        id
        name
        primaryBarcode
        thumbnailPictureUrl
        cardThumbnailPictureUrl
        prices {
          yellow { priceWithVat }
          orange { priceWithVat priceQuantity }
        }
      }
    }
  }
}
"""


class GraphQLQueryError(Exception):
    pass


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_barcode(value: Any) -> str:
    barcode = clean_text(value)
    if re.fullmatch(r"\d+\.0", barcode):
        barcode = barcode[:-2]
    return re.sub(r"[\s\u00A0\-]+", "", barcode)


def parse_price(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None

    try:
        return Decimal(str(value).replace(",", ".")).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


class DepoClient:
    def __init__(self, log_callback: Callable[[str], None] | None = None):
        self.log_callback = log_callback
        self._session: aiohttp.ClientSession | None = None
        self._rate_limit_lock = asyncio.Lock()
        self._blocked_until = 0.0
        self.pages_total = 0
        self.pages_done = 0

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def open(self):
        if self._session is not None:
            return

        connector = aiohttp.TCPConnector(limit=WORKERS + 2, ssl=False)
        timeout = aiohttp.ClientTimeout(total=120, connect=30, sock_read=90)
        self._session = aiohttp.ClientSession(
            headers=HEADERS,
            cookies=COOKIES,
            connector=connector,
            timeout=timeout,
        )

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch_categories(self) -> list[dict[str, Any]]:
        session = await self._get_session()
        data = await self._post_graphql(
            session=session,
            query=MAIN_CATEGORIES_QUERY,
            operation_name="categoriesHomepage",
            referer=f"{DEPO_WEBSITE_URL}/",
        )
        nodes = data.get("data", {}).get("categories", {}).get("nodes", [])
        categories = [
            {"id": int(node["id"])}
            for node in nodes
            if isinstance(node, dict) and node.get("id") is not None
        ]

        if not categories:
            raise RuntimeError("DEPO categories were not found.")

        return categories

    async def fetch_products(self, categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
        session = await self._get_session()
        queue: asyncio.Queue = asyncio.Queue()
        products_by_key: dict[str, dict[str, Any]] = {}
        errors: list[Exception] = []

        for index, category in enumerate(categories, start=1):
            category_id = int(category["id"])
            self._log(f"DEPO category {index}/{len(categories)}: {category_id}")
            total, rows = await self._get_products_page(session, category_id, 0)
            self._merge_products(products_by_key, rows)

            for start in range(ROWS, total, ROWS):
                await queue.put((category_id, start))
                self.pages_total += 1

        workers = [
            asyncio.create_task(self._worker(number, session, queue, products_by_key, errors))
            for number in range(1, WORKERS + 1)
        ]

        for _ in workers:
            await queue.put(None)

        await queue.join()
        await asyncio.gather(*workers)

        if errors:
            raise RuntimeError(f"DEPO product pagination failed: {errors[0]}")

        return list(products_by_key.values())

    async def _worker(self, number, session, queue, products_by_key, errors):
        while True:
            task = await queue.get()
            if task is None:
                queue.task_done()
                return

            category_id, start = task
            try:
                _, rows = await self._get_products_page(session, category_id, start)
                self._merge_products(products_by_key, rows)
                self.pages_done += 1

                if self.pages_done % 10 == 0 or self.pages_done == self.pages_total:
                    self._log(
                        f"DEPO worker {number}: pages {self.pages_done}/{self.pages_total}, "
                        f"products={len(products_by_key)}"
                    )
            except Exception as error:
                errors.append(error)
            finally:
                queue.task_done()

    async def _get_products_page(self, session, category_id: int, start: int) -> tuple[int, list[dict[str, Any]]]:
        data = await self._post_graphql(
            session=session,
            query=PRODUCTS_QUERY,
            variables={"categoryId": category_id, "rows": ROWS, "start": start},
            operation_name="products",
            referer=f"{DEPO_WEBSITE_URL}/products/{category_id}",
        )
        products_data = data.get("data", {}).get("products", {})
        total = int(products_data.get("pageInfo", {}).get("totalCount") or 0)
        rows = []

        for edge in products_data.get("edges") or []:
            product = edge.get("node") or {}
            product_id = clean_text(product.get("id"))
            prices = product.get("prices") or {}
            yellow = prices.get("yellow") or {}
            orange = prices.get("orange") or {}

            rows.append(
                {
                    "id": product_id,
                    "name": clean_text(product.get("name")),
                    "price": parse_price(yellow.get("priceWithVat")),
                    "sale_price": parse_price(orange.get("priceWithVat")),
                    "barcode": normalize_barcode(product.get("primaryBarcode")),
                    "sku": product_id,
                    "image_url": clean_text(
                        product.get("cardThumbnailPictureUrl")
                        or product.get("thumbnailPictureUrl")
                    ),
                    "product_url": f"{DEPO_WEBSITE_URL}/product/{product_id}" if product_id else "",
                    "category_id": str(category_id),
                }
            )

        return total, rows

    async def _post_graphql(
        self,
        *,
        session: aiohttp.ClientSession,
        query: str,
        variables: dict | None = None,
        operation_name: str | None = None,
        referer: str | None = None,
    ) -> dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}
        if operation_name:
            payload["operationName"] = operation_name

        headers = {"Referer": referer} if referer else {}

        for attempt in range(1, MAX_RETRIES + 1):
            await self._wait_if_blocked()
            try:
                async with session.post(GRAPHQL_URL, json=payload, headers=headers) as response:
                    if response.status == 429:
                        wait = self._retry_after(response, RETRY_WAIT + random.uniform(0, 5))
                        self._log(f"DEPO HTTP 429. Retry {attempt}/{MAX_RETRIES} in {wait:.1f}s.")
                        await self._set_global_pause(wait)
                        continue

                    if response.status >= 500:
                        wait = min(attempt * 3, 30)
                        self._log(f"DEPO HTTP {response.status}. Retry {attempt}/{MAX_RETRIES} in {wait}s.")
                        await asyncio.sleep(wait)
                        continue

                    if response.status != 200:
                        text = await response.text()
                        raise RuntimeError(f"DEPO HTTP {response.status}: {text[:300]}")

                    data = await response.json(content_type=None)
                    if data.get("errors"):
                        raise GraphQLQueryError(str(data["errors"]))

                    await asyncio.sleep(REQUEST_DELAY)
                    return data

            except GraphQLQueryError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                if attempt == MAX_RETRIES:
                    raise RuntimeError(f"DEPO request failed after {MAX_RETRIES} attempts: {error}") from error

                wait = min(attempt * 2, 30)
                self._log(f"DEPO request error: {error}. Retry {attempt}/{MAX_RETRIES} in {wait}s.")
                await asyncio.sleep(wait)

        raise RuntimeError("DEPO request failed.")

    async def _get_session(self):
        await self.open()
        if self._session is None:
            raise RuntimeError("DEPO HTTP session is not initialized.")
        return self._session

    async def _wait_if_blocked(self):
        loop = asyncio.get_running_loop()
        while True:
            async with self._rate_limit_lock:
                remaining = self._blocked_until - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    async def _set_global_pause(self, seconds: float):
        loop = asyncio.get_running_loop()
        async with self._rate_limit_lock:
            self._blocked_until = max(self._blocked_until, loop.time() + seconds)

    def _retry_after(self, response, default):
        retry_after = response.headers.get("Retry-After")
        try:
            return float(retry_after) if retry_after else default
        except (TypeError, ValueError):
            return default

    def _merge_products(self, products_by_key, rows):
        for row in rows:
            key = row["barcode"] or row["sku"] or row["product_url"]
            if not key:
                continue

            if key not in products_by_key:
                products_by_key[key] = row
                continue

            old = products_by_key[key]
            for field, value in row.items():
                if old.get(field) in ("", None) and value not in ("", None):
                    old[field] = value

    def _log(self, message: str):
        if self.log_callback:
            self.log_callback(message)
