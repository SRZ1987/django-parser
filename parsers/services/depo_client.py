from __future__ import annotations

import asyncio
import inspect
import random
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import aiohttp


GRAPHQL_URL = "https://online.depo.ee/graphql"
DEPO_WEBSITE_URL = "https://online.depo.ee"

ROWS = 20
WORKERS = 5
REQUEST_DELAY = 0.1
RETRY_WAIT = 20
MAX_RETRIES = 12
PROGRESS_LOG_INTERVAL = 15
WATCHDOG_TIMEOUT = 300

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


def parse_quantity(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        return None
    return quantity if quantity > 0 else None


class DepoClient:
    def __init__(
        self,
        log_callback: Callable[[str], None] | None = None,
        progress_log_interval: float = PROGRESS_LOG_INTERVAL,
        watchdog_timeout: float = WATCHDOG_TIMEOUT,
    ):
        self.log_callback = log_callback
        self.progress_log_interval = progress_log_interval
        self.watchdog_timeout = watchdog_timeout
        self._session: aiohttp.ClientSession | None = None
        self._rate_limit_lock = asyncio.Lock()
        self._blocked_until = 0.0
        self.pages_total = 0
        self.pages_done = 0
        self.categories_total = 0
        self.categories_done = 0
        self.retry_count = 0
        self.active_workers = 0
        self._started_at = 0.0

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
        stop_progress = asyncio.Event()
        self._started_at = asyncio.get_running_loop().time()
        self.categories_total = len(categories)
        self.categories_done = 0
        self.pages_total = len(categories)
        self.pages_done = 0
        self.retry_count = 0

        for category in categories:
            await queue.put((int(category["id"]), 0))

        workers = [
            asyncio.create_task(self._worker(number, session, queue, products_by_key, errors))
            for number in range(1, WORKERS + 1)
        ]
        progress_task = asyncio.create_task(self._progress_watchdog(queue, products_by_key, stop_progress))

        try:
            join_task = asyncio.create_task(queue.join())
            done, pending = await asyncio.wait(
                {join_task, progress_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done:
                exception = task.exception()
                if exception:
                    raise exception

            if join_task in pending:
                join_task.cancel()
                await asyncio.gather(join_task, return_exceptions=True)
            elif progress_task in pending:
                stop_progress.set()
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)

            if errors:
                raise RuntimeError(f"DEPO product pagination failed: {errors[0]}")

            return list(products_by_key.values())
        finally:
            stop_progress.set()
            progress_task.cancel()
            for worker in workers:
                worker.cancel()
            await asyncio.gather(progress_task, *workers, return_exceptions=True)

    async def _worker(self, number, session, queue, products_by_key, errors):
        while True:
            task = await queue.get()
            try:
                self.active_workers += 1
                category_id, start = task
                total, rows = await self._get_products_page(session, category_id, start)
                if start == 0:
                    self.categories_done += 1
                    page_starts = list(range(ROWS, total, ROWS))
                    self.pages_total += len(page_starts)
                    for next_start in page_starts:
                        await queue.put((category_id, next_start))
                    await self._log(
                        f"DEPO category {self.categories_done}/{self.categories_total}: "
                        f"{category_id}, products={total}"
                    )
                self._merge_products(products_by_key, rows)
                self.pages_done += 1

                if self.pages_done % 10 == 0 or self.pages_done == self.pages_total:
                    await self._log(
                        f"DEPO worker {number}: pages {self.pages_done}/{self.pages_total}, "
                        f"products={len(products_by_key)}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                errors.append(error)
                await self._log(f"DEPO worker {number} failed: {error}")
            finally:
                self.active_workers = max(0, self.active_workers - 1)
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
                    "sale_price": None,
                    "quantity_price": parse_price(orange.get("priceWithVat")),
                    "quantity_price_min_quantity": parse_quantity(orange.get("priceQuantity")),
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
                        self.retry_count += 1
                        wait = self._retry_after(response, RETRY_WAIT + random.uniform(0, 5))
                        await self._log(f"DEPO HTTP 429. Retry {attempt}/{MAX_RETRIES} in {wait:.1f}s.")
                        await self._set_global_pause(wait)
                        continue

                    if response.status >= 500:
                        self.retry_count += 1
                        wait = min(attempt * 3, 30)
                        await self._log(f"DEPO HTTP {response.status}. Retry {attempt}/{MAX_RETRIES} in {wait}s.")
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

                self.retry_count += 1
                wait = min(attempt * 2, 30)
                await self._log(f"DEPO request error: {error}. Retry {attempt}/{MAX_RETRIES} in {wait}s.")
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

    async def _progress_watchdog(self, queue, products_by_key, stop_event):
        loop = asyncio.get_running_loop()
        last_progress_at = loop.time()
        last_pages_done = self.pages_done
        last_products_count = len(products_by_key)
        last_queue_size = queue.qsize()

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.progress_log_interval)
                break
            except asyncio.TimeoutError:
                pass

            now = loop.time()
            queue_size = queue.qsize()
            products_count = len(products_by_key)
            if (
                self.pages_done > last_pages_done
                or products_count > last_products_count
                or queue_size < last_queue_size
            ):
                last_progress_at = now

            last_pages_done = self.pages_done
            last_products_count = products_count
            last_queue_size = queue_size

            await self._log(
                "DEPO download progress: categories={categories_done}/{categories_total}, "
                "pages={pages_done}/{pages_total}, products={products}, queue={queue}, "
                "workers={workers}, retries={retries}, elapsed={elapsed}".format(
                    categories_done=self.categories_done,
                    categories_total=self.categories_total,
                    pages_done=self.pages_done,
                    pages_total=self.pages_total,
                    products=products_count,
                    queue=queue_size,
                    workers=self.active_workers,
                    retries=self.retry_count,
                    elapsed=self._format_elapsed(now - self._started_at),
                )
            )

            if now - last_progress_at >= self.watchdog_timeout:
                raise RuntimeError(
                    "DEPO download stalled: no pages, products, or queue progress for "
                    f"{int(self.watchdog_timeout)} seconds."
                )

    def _format_elapsed(self, seconds):
        total_seconds = int(seconds)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    async def _log(self, message: str):
        if self.log_callback:
            result = self.log_callback(message)
            if inspect.isawaitable(result):
                await result
