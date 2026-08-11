from __future__ import annotations

import asyncio
import html
import math
import random
import re
from pathlib import Path
from typing import Any

import aiohttp
import pandas as pd


# ============================================================
# НАСТРОЙКИ
# ============================================================

API_URL = "https://espak.ee/epood/wp-json/wc/store/v1/products"
OUTPUT_FILE = Path("espak.xlsx")

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
    "Referer": "https://espak.ee/epood/",
}

COLUMNS = [
    "Название товара",
    "Цена",
    "Цена со скидкой",
    "Цена со скидкой 2",
    "Штрихкод",
    "Код магазина",
    "Фото",
    "Ссылка",
    "SKU",
    "Category",
    "Category ID",
    "Description",
    "Brand",
    "Model",
]

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


# ============================================================
# ОБРАБОТКА ДАННЫХ
# ============================================================

def clean_text(value: Any) -> str:
    if value in (None, ""):
        return ""

    text = html.unescape(str(value))
    text = TAG_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def money_to_float(value: Any, minor_unit: int = 2) -> float | None:
    if value in (None, ""):
        return None

    try:
        return int(str(value)) / (10 ** int(minor_unit))
    except (TypeError, ValueError):
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


def parse_product(product: dict[str, Any]) -> dict[str, Any]:
    prices = product.get("prices") or {}

    try:
        minor_unit = int(prices.get("currency_minor_unit", 2) or 2)
    except (TypeError, ValueError):
        minor_unit = 2

    regular_price = money_to_float(
        prices.get("regular_price"),
        minor_unit,
    )
    sale_price = money_to_float(
        prices.get("sale_price"),
        minor_unit,
    )

    on_sale = bool(product.get("on_sale"))
    discount_price = sale_price if on_sale and sale_price is not None else ""

    images = product.get("images") or []
    image_url = ""

    for image in images:
        if isinstance(image, dict) and image.get("src"):
            image_url = clean_text(image.get("src"))
            break

    barcode = get_attribute(
        product,
        "Ribakood",
        "EAN",
        "GTIN",
        "Barcode",
        "Штрихкод",
    )
    categories = [item for item in product.get("categories") or [] if isinstance(item, dict)]
    category = categories[-1] if categories else {}
    sku = clean_text(product.get("sku"))

    return {
        "Название товара": clean_text(product.get("name")),
        "Цена": regular_price if regular_price is not None else "",
        "Цена со скидкой": discount_price,
        "Цена со скидкой 2": "",
        "Штрихкод": barcode,
        "Код магазина": sku,
        "Фото": image_url,
        "Ссылка": clean_text(product.get("permalink")),
        "SKU": sku,
        "Category": clean_text(category.get("name")),
        "Category ID": clean_text(category.get("id")),
        "Description": clean_text(product.get("description") or product.get("short_description")),
        "Brand": get_attribute(product, "Kaubamärk", "Brand", "Tootja", "Manufacturer"),
        "Model": get_attribute(product, "Mudel", "Model", "Tootekood"),
    }


# ============================================================
# HTTP
# ============================================================

async def request_json(
    session: aiohttp.ClientSession,
    params: dict[str, Any],
) -> tuple[list[dict[str, Any]], aiohttp.typedefs.LooseHeaders]:

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(API_URL, params=params) as response:
                if response.status == 200:
                    data = await response.json(content_type=None)

                    if not isinstance(data, list):
                        raise RuntimeError(
                            f"API вернул {type(data).__name__}, ожидался список"
                        )

                    return data, response.headers

                if response.status in {429, 500, 502, 503, 504}:
                    retry_after = response.headers.get("Retry-After")

                    try:
                        delay = float(retry_after) if retry_after else 0
                    except (TypeError, ValueError):
                        delay = 0

                    if delay <= 0:
                        delay = (
                            RETRY_BASE_DELAY * (2 ** (attempt - 1))
                            + random.uniform(0.2, 1.2)
                        )

                    print(
                        f"HTTP {response.status}. "
                        f"Повтор {attempt}/{MAX_RETRIES} через {delay:.1f} сек."
                    )
                    await asyncio.sleep(delay)
                    continue

                body = await response.text()
                raise RuntimeError(
                    f"HTTP {response.status}: {body[:300]}"
                )

        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as error:
            if attempt == MAX_RETRIES:
                raise RuntimeError(
                    f"Запрос не выполнен после {MAX_RETRIES} попыток: {error}"
                ) from error

            delay = (
                RETRY_BASE_DELAY * (2 ** (attempt - 1))
                + random.uniform(0.2, 1.2)
            )

            print(
                f"Ошибка запроса: {error}. "
                f"Повтор {attempt}/{MAX_RETRIES} через {delay:.1f} сек."
            )
            await asyncio.sleep(delay)

    raise RuntimeError("Не удалось выполнить запрос")


async def fetch_page(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    page: int,
) -> tuple[int, list[dict[str, Any]]]:

    async with semaphore:
        products, _ = await request_json(
            session,
            {
                "page": page,
                "per_page": PER_PAGE,
                "orderby": "id",
                "order": "asc",
            },
        )

    return page, products


# ============================================================
# ЗАГРУЗКА КАТАЛОГА
# ============================================================

async def collect_until_empty(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    first_page: list[dict[str, Any]],
) -> list[dict[str, Any]]:

    all_products = list(first_page)
    next_page = 2

    while True:
        page_numbers = list(range(next_page, next_page + CONCURRENCY))

        results = await asyncio.gather(
            *[
                fetch_page(session, semaphore, page)
                for page in page_numbers
            ]
        )
        results.sort(key=lambda item: item[0])

        found_empty = False

        for page, products in results:
            if not products:
                found_empty = True
                break

            all_products.extend(products)
            print(
                f"Страница {page} загружена. "
                f"Сырых товаров: {len(all_products)}"
            )

        if found_empty:
            break

        next_page += CONCURRENCY

    return all_products


async def collect_all_products() -> list[dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

    connector = aiohttp.TCPConnector(
        limit=CONCURRENCY,
        limit_per_host=CONCURRENCY,
        ttl_dns_cache=300,
    )

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with aiohttp.ClientSession(
        headers=HEADERS,
        timeout=timeout,
        connector=connector,
    ) as session:

        print("Получаем первую страницу...")

        first_page, headers = await request_json(
            session,
            {
                "page": 1,
                "per_page": PER_PAGE,
                "orderby": "id",
                "order": "asc",
            },
        )

        total_pages_raw = headers.get("X-WP-TotalPages")
        total_items_raw = headers.get("X-WP-Total")

        if total_pages_raw and str(total_pages_raw).isdigit():
            total_pages = int(total_pages_raw)
        elif total_items_raw and str(total_items_raw).isdigit():
            total_pages = math.ceil(int(total_items_raw) / PER_PAGE)
        else:
            print(
                "API не вернул количество страниц. "
                "Загружаем до первой пустой страницы."
            )
            return await collect_until_empty(
                session,
                semaphore,
                first_page,
            )

        print(
            f"Товаров: {total_items_raw or 'неизвестно'} | "
            f"страниц: {total_pages}"
        )

        if total_pages <= 1:
            return first_page

        all_products = list(first_page)

        tasks = [
            asyncio.create_task(
                fetch_page(session, semaphore, page)
            )
            for page in range(2, total_pages + 1)
        ]

        completed = 1

        for task in asyncio.as_completed(tasks):
            page, products = await task
            all_products.extend(products)
            completed += 1

            print(
                f"Страницы: {completed}/{total_pages} | "
                f"товаров: {len(all_products)}"
            )

            if not products:
                print(f"Внимание: страница {page} пустая.")

        return all_products


# ============================================================
# СОХРАНЕНИЕ
# ============================================================

def remove_duplicates(
    raw_products: list[dict[str, Any]],
) -> list[dict[str, Any]]:

    unique: dict[Any, dict[str, Any]] = {}

    for product in raw_products:
        key = (
            product.get("id")
            or product.get("permalink")
            or product.get("sku")
            or product.get("name")
        )
        unique[key] = product

    return list(unique.values())


def save_to_excel(products: list[dict[str, Any]]) -> None:
    rows = [parse_product(product) for product in products]
    dataframe = pd.DataFrame(rows, columns=COLUMNS)

    dataframe.to_excel(
        OUTPUT_FILE,
        index=False,
        engine="openpyxl",
    )

    print(f"Excel сохранён: {OUTPUT_FILE.resolve()}")


# ============================================================
# ЗАПУСК
# ============================================================

async def main() -> None:
    print("Запуск парсера ESPAK")

    raw_products = await collect_all_products()
    print(f"Получено сырых товаров: {len(raw_products)}")

    unique_products = remove_duplicates(raw_products)
    print(f"Уникальных товаров: {len(unique_products)}")

    save_to_excel(unique_products)
    print("Готово")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Остановлено пользователем")
    except Exception as error:
        print(f"Критическая ошибка: {error}")
        raise
