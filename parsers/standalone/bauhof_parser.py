import asyncio
import contextlib
import html
import io
import json
import random
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
import pandas as pd

BASE_URL = "https://www.bauhof.ee"
SITEMAP_INDEX_URL = f"{BASE_URL}/sitemap.xml"
PRODUCT_API_URL = f"{BASE_URL}/api/magento/products?locale=et"
OUTPUT_FILE = Path("bauhof.xlsx")

SITEMAP_WORKERS = 5
API_BATCH_SIZE = 100
API_WORKERS = 8
REQUEST_TIMEOUT = 120
MAX_RETRIES = 8

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

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

COMMON_HEADERS = {
    "user-agent": USER_AGENT,
    "accept-language": "et-EE,et;q=0.9,en;q=0.8,ru;q=0.7",
    "cache-control": "no-cache",
    "pragma": "no-cache",
}

SITEMAP_HEADERS = {
    **COMMON_HEADERS,
    "accept": "application/xml,text/xml,text/html;q=0.9,*/*;q=0.8",
}

API_HEADERS = {
    **COMMON_HEADERS,
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": BASE_URL,
    "referer": BASE_URL + "/",
}

LOC_PATTERN = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", html.unescape(str(value))).strip()


def clean_barcode(value: Any) -> str:
    barcode = clean_text(value)
    if re.fullmatch(r"\d+\.0", barcode):
        barcode = barcode[:-2]
    return re.sub(r"[\s\u00A0\-]+", "", barcode)


def clean_price(value: Any) -> float | str:
    if value in (None, ""):
        return ""
    try:
        return round(float(str(value).replace(",", ".")), 2)
    except (TypeError, ValueError):
        return ""


def nested(data: Any, *keys: str, default: Any = None) -> Any:
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def batches(values: list[str], size: int) -> list[list[str]]:
    return [values[i:i + size] for i in range(0, len(values), size)]


async def request_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
    method: str = "GET",
    payload: Any = None,
    headers: dict[str, str] | None = None,
) -> str:
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = aiohttp.ClientTimeout(
                total=REQUEST_TIMEOUT,
                connect=30,
                sock_read=REQUEST_TIMEOUT,
            )

            async with session.request(
                method,
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
            ) as response:
                text = await response.text()

                if response.status == 200:
                    return text

                if response.status in {408, 425, 429, 500, 502, 503, 504}:
                    try:
                        delay = float(response.headers.get("Retry-After", 0))
                    except ValueError:
                        delay = 0

                    if delay <= 0:
                        delay = min(60, 2 ** attempt + random.uniform(0.5, 2.5))

                    print(f"HTTP {response.status}. Повтор через {delay:.1f} сек.")
                    await asyncio.sleep(delay)
                    continue

                raise RuntimeError(f"HTTP {response.status}: {text[:500]}")

        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as error:
            last_error = error
            if attempt >= MAX_RETRIES:
                break

            delay = min(60, 2 ** attempt + random.uniform(0.5, 2.5))
            print(f"Ошибка запроса: {error}. Повтор через {delay:.1f} сек.")
            await asyncio.sleep(delay)

    raise RuntimeError(f"Запрос не выполнен: {url}. Последняя ошибка: {last_error}")


async def request_json(session: aiohttp.ClientSession, payload: Any) -> Any:
    text = await request_text(
        session,
        PRODUCT_API_URL,
        method="POST",
        payload=payload,
        headers=API_HEADERS,
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"API вернул некорректный JSON: {text[:1000]}") from error


def extract_locations(xml_text: str) -> list[str]:
    return [
        html.unescape(location).strip()
        for location in LOC_PATTERN.findall(xml_text)
        if location.strip()
    ]


def extract_product(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    match = re.match(r"^/et/p/([^/]+)(?:/.*)?$", path, re.I)

    if not match:
        return None

    sku = clean_text(match.group(1))
    if not sku:
        return None

    return sku, f"{parsed.scheme}://{parsed.netloc}{path}"


async def get_sku_map(session: aiohttp.ClientSession) -> dict[str, str]:
    index_xml = await request_text(
        session,
        SITEMAP_INDEX_URL,
        headers=SITEMAP_HEADERS,
    )

    sitemap_urls = sorted({
        url
        for url in extract_locations(index_xml)
        if "/sitemaps/products-et/" in url.lower()
        and url.lower().endswith(".xml")
    })

    if not sitemap_urls:
        raise RuntimeError("Товарные sitemap не найдены.")

    semaphore = asyncio.Semaphore(SITEMAP_WORKERS)
    sku_map: dict[str, str] = {}
    lock = asyncio.Lock()
    completed = 0

    async def process(url: str) -> None:
        nonlocal completed
        async with semaphore:
            xml_text = await request_text(session, url, headers=SITEMAP_HEADERS)

        local: dict[str, str] = {}
        for product_url in extract_locations(xml_text):
            result = extract_product(product_url)
            if result:
                sku, clean_url = result
                local[sku] = clean_url

        async with lock:
            sku_map.update(local)
            completed += 1
            print(f"Sitemap {completed}/{len(sitemap_urls)} | SKU: {len(sku_map)}")

    await asyncio.gather(*(process(url) for url in sitemap_urls))
    return sku_map


def build_payload(skus: list[str]) -> list[dict[str, Any]]:
    return [{
        "filter": {"sku": {"in": skus}},
        "pageSize": len(skus),
        "currentPage": 1,
    }]


def extract_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        result: list[dict[str, Any]] = []
        for element in data:
            result.extend(extract_items(element))
        return result

    items = nested(data, "data", "products", "items", default=[])
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def get_price(item: dict[str, Any], price_type: str) -> float | str:
    minimum = nested(
        item, "price_range", "minimum_price", price_type, "value", default=None
    )
    maximum = nested(
        item, "price_range", "maximum_price", price_type, "value", default=None
    )
    return clean_price(minimum if minimum is not None else maximum)


def build_url(item: dict[str, Any], sitemap_url: str) -> str:
    if sitemap_url:
        return sitemap_url

    sku = clean_text(item.get("sku"))
    url_key = clean_text(item.get("url_key"))

    if sku and url_key:
        return f"{BASE_URL}/et/p/{sku}/{url_key}"
    if sku:
        return f"{BASE_URL}/et/p/{sku}"
    return ""


def product_to_row(item: dict[str, Any], sitemap_url: str) -> dict[str, Any]:
    regular_price = get_price(item, "regular_price")
    final_price = get_price(item, "final_price")

    discount_price: float | str = ""
    if regular_price != "" and final_price != "" and float(final_price) < float(regular_price):
        discount_price = final_price

    return {
        "Название товара": clean_text(item.get("name")),
        "Цена": regular_price,
        "Цена со скидкой": discount_price,
        "Цена со скидкой 2": "",
        "Штрихкод": clean_barcode(item.get("barcode")),
        "Код магазина": clean_text(item.get("sku")),
        "Фото": clean_text(nested(item, "thumbnail", "url", default="")),
        "Ссылка": build_url(item, sitemap_url),
    }


async def collect_products(
    session: aiohttp.ClientSession,
    sku_map: dict[str, str],
) -> dict[str, dict[str, Any]]:
    all_batches = batches(sorted(sku_map), API_BATCH_SIZE)
    queue: asyncio.Queue[tuple[int, list[str]] | None] = asyncio.Queue()

    for number, batch in enumerate(all_batches, start=1):
        await queue.put((number, batch))

    products: dict[str, dict[str, Any]] = {}
    lock = asyncio.Lock()
    completed = 0

    async def worker(number: int) -> None:
        nonlocal completed

        while True:
            task = await queue.get()
            if task is None:
                queue.task_done()
                return

            batch_number, batch = task

            try:
                response = await request_json(session, build_payload(batch))
                rows: dict[str, dict[str, Any]] = {}

                for item in extract_items(response):
                    sku = clean_text(item.get("sku"))
                    if sku:
                        rows[sku] = product_to_row(item, sku_map.get(sku, ""))

                async with lock:
                    products.update(rows)
                    completed += 1
                    if completed % 10 == 0 or completed == len(all_batches):
                        print(
                            f"Пачек {completed}/{len(all_batches)} | "
                            f"товаров {len(products)}"
                        )

            except Exception as error:
                print(f"Воркер {number}, пачка {batch_number}: {error}")

            finally:
                queue.task_done()

    workers = [
        asyncio.create_task(worker(number))
        for number in range(1, API_WORKERS + 1)
    ]

    await queue.join()
    for _ in workers:
        await queue.put(None)
    await asyncio.gather(*workers)

    return products


def save_excel(products: dict[str, dict[str, Any]]) -> None:
    dataframe = pd.DataFrame(products.values(), columns=COLUMNS)

    if dataframe.empty:
        print("Нет товаров для сохранения.")
        return

    dataframe = dataframe.fillna("")
    dataframe = dataframe.drop_duplicates(subset=["Код магазина"], keep="last")
    dataframe = dataframe.sort_values(
        by=["Название товара", "Код магазина"],
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
            "H": 90,
        }
        for column, width in widths.items():
            worksheet.column_dimensions[column].width = width

        for row in range(2, worksheet.max_row + 1):
            worksheet[f"E{row}"].number_format = "@"
            worksheet[f"F{row}"].number_format = "@"
            for column in ("B", "C", "D"):
                worksheet[f"{column}{row}"].number_format = "0.00"

    print(f"Готово: {OUTPUT_FILE.resolve()} | товаров: {len(dataframe)}")


async def _main() -> None:
    connector = aiohttp.TCPConnector(
        ssl=False,
        limit=40,
        limit_per_host=20,
        ttl_dns_cache=600,
        enable_cleanup_closed=True,
    )

    async with aiohttp.ClientSession(
        connector=connector,
        headers=COMMON_HEADERS,
    ) as session:
        sku_map = await get_sku_map(session)
        print(f"Всего SKU: {len(sku_map)}")
        products = await collect_products(session, sku_map)

    await asyncio.to_thread(save_excel, products)


async def main(output_path: str | Path | None = None, log_callback=None) -> None:
    global OUTPUT_FILE

    original_output_file = OUTPUT_FILE
    if output_path is not None:
        OUTPUT_FILE = Path(output_path)
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    writer = CallbackWriter(log_callback)
    try:
        if log_callback is None:
            await _main()
        else:
            with contextlib.redirect_stdout(writer):
                await _main()
            writer.flush()
    finally:
        OUTPUT_FILE = original_output_file


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nПарсер остановлен.")
    except Exception as error:
        print(f"\nКритическая ошибка: {type(error).__name__}: {error}")
        raise
