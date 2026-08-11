import asyncio
import contextlib
import io
import random
import re
from pathlib import Path
from typing import Any

import aiohttp
import pandas as pd

GRAPHQL_URL = "https://online.depo.ee/graphql"
SITE_URL = "https://online.depo.ee"

ROWS = 20
WORKERS = 5
REQUEST_DELAY = 0.1
RETRY_WAIT = 20
MAX_RETRIES = 12
OUTPUT_FILE = "depo.xlsx"

COLUMNS = [
    "Название товара",
    "Цена",
    "Цена со скидкой",
    "Цена со скидкой 2",
    "Штрихкод",
    "Код магазина",
    "Фото",
    "Ссылка",
    "Минимальное количество для скидки",
    "SKU",
    "Category",
    "Category ID",
    "Description",
    "Brand",
    "Model",
]


class CallbackWriter(io.TextIOBase):
    def __init__(self, callback):
        self.callback = callback
        self._buffer = ""

    def write(self, text):
        if not text:
            return 0
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if line and self.callback:
                self.callback(line)
        return len(text)

    def flush(self):
        line = self._buffer.strip()
        if line and self.callback:
            self.callback(line)
        self._buffer = ""

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": SITE_URL,
    "Referer": f"{SITE_URL}/",
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
        breadcrumb {
          categoryBreadcrumb {
            id
            name
            parentCategoryId
          }
        }
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


class DepoParser:
    def __init__(self) -> None:
        self.products: dict[str, dict[str, Any]] = {}
        self.products_lock = asyncio.Lock()
        self.rate_limit_lock = asyncio.Lock()
        self.blocked_until = 0.0
        self.pages_total = 0
        self.pages_done = 0
        self.categories_total = 0
        self.categories_done = 0
        self.errors: list[Exception] = []

    @staticmethod
    def normalize_text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def normalize_barcode(value: Any) -> str:
        barcode = str(value or "").strip()
        if re.fullmatch(r"\d+\.0", barcode):
            barcode = barcode[:-2]
        return re.sub(r"[\s\u00A0\-]+", "", barcode)

    @staticmethod
    def normalize_price(value: Any) -> float | str:
        if value in (None, ""):
            return ""
        try:
            return round(float(str(value).replace(",", ".")), 2)
        except (TypeError, ValueError):
            return ""

    @staticmethod
    def normalize_quantity(value: Any) -> int | str:
        if value in (None, "") or isinstance(value, bool):
            return ""
        try:
            quantity = int(value)
        except (TypeError, ValueError):
            return ""
        return quantity if quantity > 0 else ""

    async def wait_if_blocked(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            async with self.rate_limit_lock:
                remaining = self.blocked_until - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    async def set_global_pause(self, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        async with self.rate_limit_lock:
            self.blocked_until = max(self.blocked_until, loop.time() + seconds)

    async def post_graphql(
        self,
        session: aiohttp.ClientSession,
        query: str,
        variables: dict | None = None,
        operation_name: str | None = None,
        referer: str | None = None,
    ) -> dict | None:
        payload = {"query": query, "variables": variables or {}}
        if operation_name:
            payload["operationName"] = operation_name

        headers = {"Referer": referer} if referer else {}

        for attempt in range(1, MAX_RETRIES + 1):
            await self.wait_if_blocked()
            try:
                async with session.post(
                    GRAPHQL_URL,
                    json=payload,
                    headers=headers,
                ) as response:
                    if response.status == 429:
                        retry_after = response.headers.get("Retry-After", "")
                        try:
                            wait = max(float(retry_after), 0.0)
                        except ValueError:
                            wait = RETRY_WAIT + random.uniform(0, 5)
                        print(f"429: пауза {wait:.1f} сек.")
                        await self.set_global_pause(wait)
                        continue

                    if response.status >= 500:
                        wait = min(attempt * 3, 30)
                        print(
                            f"HTTP {response.status}. "
                            f"Попытка {attempt}/{MAX_RETRIES}. Ждём {wait} сек."
                        )
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
                wait = min(attempt * 2, 30)
                print(
                    f"Ошибка запроса: {error}. "
                    f"Попытка {attempt}/{MAX_RETRIES}. Ждём {wait} сек."
                )
                await asyncio.sleep(wait)

        raise RuntimeError(f"DEPO request failed after {MAX_RETRIES} attempts.")

    async def get_categories(self, session: aiohttp.ClientSession) -> list[int]:
        data = await self.post_graphql(
            session=session,
            query=MAIN_CATEGORIES_QUERY,
            operation_name="categoriesHomepage",
            referer=f"{SITE_URL}/",
        )
        nodes = (
            (data or {})
            .get("data", {})
            .get("categories", {})
            .get("nodes", [])
        )
        return [int(node["id"]) for node in nodes if node.get("id") is not None]

    async def get_products_page(
        self,
        session: aiohttp.ClientSession,
        category_id: int,
        start: int,
    ) -> tuple[int, list[dict]]:
        data = await self.post_graphql(
            session=session,
            query=PRODUCTS_QUERY,
            variables={"categoryId": category_id, "rows": ROWS, "start": start},
            operation_name="products",
            referer=f"{SITE_URL}/products/{category_id}",
        )

        products_data = (data or {}).get("data", {}).get("products", {})
        total = int(products_data.get("pageInfo", {}).get("totalCount") or 0)
        rows = []

        for edge in products_data.get("edges") or []:
            product = edge.get("node") or {}
            product_id = self.normalize_text(product.get("id"))
            category_breadcrumb = (
                (product.get("breadcrumb") or {}).get("categoryBreadcrumb")
                or []
            )
            product_category = category_breadcrumb[-1] if category_breadcrumb else {}
            prices = product.get("prices") or {}
            yellow = prices.get("yellow") or {}
            orange = prices.get("orange") or {}

            rows.append({
                "Название товара": self.normalize_text(product.get("name")),
                "Цена": self.normalize_price(yellow.get("priceWithVat")),
                "Цена со скидкой": "",
                "Цена со скидкой 2": self.normalize_price(orange.get("priceWithVat")),
                "Штрихкод": self.normalize_barcode(product.get("primaryBarcode")),
                "Код магазина": product_id,
                "Фото": self.normalize_text(
                    product.get("cardThumbnailPictureUrl")
                    or product.get("thumbnailPictureUrl")
                ),
                "Ссылка": f"{SITE_URL}/product/{product_id}" if product_id else "",
                "Минимальное количество для скидки": self.normalize_quantity(orange.get("priceQuantity")),
                "SKU": product_id,
                "Category": self.normalize_text(product_category.get("name")),
                "Category ID": (
                    self.normalize_text(product_category.get("id"))
                    or str(category_id)
                ),
                "Description": "",
                "Brand": "",
                "Model": "",
            })

        return total, rows

    async def add_products(self, rows: list[dict]) -> None:
        async with self.products_lock:
            for row in rows:
                key = row["Штрихкод"] or row["Код магазина"] or row["Ссылка"]
                if not key:
                    continue

                if key not in self.products:
                    self.products[key] = row
                    continue

                old = self.products[key]
                for field in COLUMNS:
                    if old.get(field) in ("", None) and row.get(field) not in ("", None):
                        old[field] = row[field]

    async def prepare_queue(
        self,
        session: aiohttp.ClientSession,
        categories: list[int],
        queue: asyncio.Queue,
    ) -> None:
        self.categories_total = len(categories)
        self.pages_total = len(categories)
        for category_id in categories:
            await queue.put((category_id, 0))

    async def worker(
        self,
        number: int,
        session: aiohttp.ClientSession,
        queue: asyncio.Queue,
    ) -> None:
        while True:
            task = await queue.get()
            category_id, start = task
            try:
                total, rows = await self.get_products_page(session, category_id, start)
                if start == 0:
                    self.categories_done += 1
                    page_starts = list(range(ROWS, total, ROWS))
                    self.pages_total += len(page_starts)
                    for next_start in page_starts:
                        await queue.put((category_id, next_start))
                    print(
                        f"Категории {self.categories_done}/{self.categories_total} | "
                        f"категория {category_id}: {total} товаров"
                    )
                await self.add_products(rows)
                self.pages_done += 1

                if self.pages_done % 10 == 0 or self.pages_done == self.pages_total:
                    print(
                        f"Воркер {number} | страниц {self.pages_done}/{self.pages_total} "
                        f"| товаров {len(self.products)}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.errors.append(error)
                print(f"Воркер {number}: {error}")
            finally:
                queue.task_done()

    def save_excel(self) -> None:
        dataframe = pd.DataFrame(self.products.values(), columns=COLUMNS)
        if dataframe.empty:
            print("Нет товаров для сохранения.")
            return

        dataframe = dataframe.fillna("").sort_values(
            by=["Название товара", "Штрихкод"],
            na_position="last",
        )

        with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
            dataframe.to_excel(writer, sheet_name="Товары", index=False)
            worksheet = writer.sheets["Товары"]
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions

            widths = {
                "A": 65,
                "B": 14,
                "C": 18,
                "D": 20,
                "E": 22,
                "F": 20,
                "G": 70,
                "H": 60,
                "I": 24,
            }
            for column, width in widths.items():
                worksheet.column_dimensions[column].width = width

            for row in range(2, worksheet.max_row + 1):
                worksheet[f"E{row}"].number_format = "@"
                worksheet[f"F{row}"].number_format = "@"
                for column in ("B", "C", "D"):
                    worksheet[f"{column}{row}"].number_format = "0.00"
                worksheet[f"I{row}"].number_format = "0"

        print(f"Готово: {OUTPUT_FILE}")
        print(f"Товаров: {len(dataframe)}")


async def main_async() -> None:
    parser = DepoParser()
    connector = aiohttp.TCPConnector(limit=WORKERS + 2, ssl=False)
    timeout = aiohttp.ClientTimeout(total=120, connect=30, sock_read=90)

    async with aiohttp.ClientSession(
        headers=HEADERS,
        cookies=COOKIES,
        connector=connector,
        timeout=timeout,
    ) as session:
        categories = await parser.get_categories(session)
        if not categories:
            print("Категории не найдены.")
            return

        queue: asyncio.Queue = asyncio.Queue()
        await parser.prepare_queue(session, categories, queue)

        workers = [
            asyncio.create_task(parser.worker(number, session, queue))
            for number in range(1, WORKERS + 1)
        ]
        try:
            await queue.join()
        finally:
            for worker_task in workers:
                worker_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        if parser.errors:
            raise RuntimeError(f"DEPO catalog download is incomplete: {parser.errors[0]}")
        if not parser.products:
            raise RuntimeError("DEPO returned an empty product catalog.")

    await asyncio.to_thread(parser.save_excel)


async def main(output_path: str | Path | None = None, log_callback=None) -> None:
    global OUTPUT_FILE

    original_output_file = OUTPUT_FILE
    if output_path is not None:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_FILE = str(output_path)

    writer = CallbackWriter(log_callback)
    try:
        if log_callback is None:
            await main_async()
        else:
            with contextlib.redirect_stdout(writer):
                await main_async()
            writer.flush()
    finally:
        OUTPUT_FILE = original_output_file


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nПарсер остановлен.")
    except GraphQLQueryError as error:
        print(f"\nОшибка GraphQL: {error}")
    except Exception as error:
        print(f"\nКритическая ошибка: {error}")
        raise
