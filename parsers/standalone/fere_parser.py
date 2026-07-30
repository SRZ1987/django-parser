from __future__ import annotations

import asyncio
import contextlib
import html
import io
import random
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import aiohttp
import pandas as pd
from bs4 import BeautifulSoup, Tag


# ============================================================
# НАСТРОЙКИ
# ============================================================

BASE_URL = "https://fere.ee/"
OUTPUT_FILE = Path("fere.xlsx")

CONCURRENCY = 6
REQUEST_TIMEOUT = 45
MAX_RETRIES = 6
RETRY_BASE_DELAY = 2.0

REQUEST_DELAY_MIN = 0.10
REQUEST_DELAY_MAX = 0.35

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "et-EE,et;q=0.9,en;q=0.8,ru;q=0.7",
    "Referer": BASE_URL,
    "Cache-Control": "no-cache",
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

IGNORED_PATH_PARTS = {
    "/customer/",
    "/checkout/",
    "/catalogsearch/",
    "/contacts/",
    "/blog/",
    "/rss/",
    "/sales/",
    "/wishlist/",
    "/review/",
    "/sendfriend/",
    "/tag/",
    "/media/",
    "/skin/",
    "/js/",
}

IGNORED_EXACT_PATHS = {
    "/",
    "/ettevottest",
    "/e-poe-muugitingimused",
    "/e_poe-muugitingimused",
    "/privaatsuspoliitika",
    "/kaubamargid",
    "/kontakt",
    "/kontaktandmed",
    "/asukoht",
    "/tagasiside",
    "/elektroonikajaatmed",
    "/tuletoole",
}

PLACEHOLDER_MARKERS = (
    "/placeholder/",
    "small_image.jpg",
    "placeholder.jpg",
)

SPACE_RE = re.compile(r"\s+")
NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
EAN_LABEL_RE = re.compile(
    r"^\s*(?:EAN|GTIN|BARCODE|RIBAKOOD|ШТРИХКОД)\s*:?\s*",
    flags=re.IGNORECASE,
)
SKU_LABEL_RE = re.compile(
    r"^\s*(?:TOOTEKOOD|PRODUCT\s*CODE|ARTIKKEL|АРТИКУЛ|КОД\s*ТОВАРА)\s*:?\s*",
    flags=re.IGNORECASE,
)


# ============================================================
# ТЕКСТ, ЦЕНЫ И URL
# ============================================================

def clean_text(value: Any) -> str:
    if value in (None, ""):
        return ""

    if isinstance(value, Tag):
        value = value.get_text(" ", strip=True)

    text = html.unescape(str(value)).replace("\xa0", " ")
    return SPACE_RE.sub(" ", text).strip()


def text_of(element: Tag | None) -> str:
    if element is None:
        return ""
    return clean_text(element.get_text(" ", strip=True))


def parse_price(value: Any) -> float | str:
    text = clean_text(value)

    if not text:
        return ""

    normalized = (
        text.replace("\xa0", "")
        .replace("€", "")
        .replace("EUR", "")
        .replace(" ", "")
        .replace(",", ".")
    )

    match = NUMBER_RE.search(normalized)

    if not match:
        return ""

    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return ""


def normalize_ean(value: Any) -> str:
    text = EAN_LABEL_RE.sub("", clean_text(value))
    return re.sub(r"\D", "", text)


def normalize_sku(value: Any) -> str:
    return SKU_LABEL_RE.sub("", clean_text(value)).strip()


def normalize_url(url: Any, base_url: str = BASE_URL) -> str:
    if not url:
        return ""

    absolute = urljoin(base_url, html.unescape(str(url)))
    parsed = urlparse(absolute)

    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {
            "___store",
            "___from_store",
            "dir",
            "order",
            "mode",
            "limit",
            "hidepictures",
        }
    ]

    return urlunparse(
        (
            parsed.scheme.lower() or "https",
            parsed.netloc.lower(),
            parsed.path or "/",
            "",
            urlencode(query),
            "",
        )
    )


def set_page(url: str, page: int) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    if page <= 1:
        query.pop("p", None)
    else:
        query["p"] = str(page)

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query),
            "",
        )
    )


def is_internal_url(url: str) -> bool:
    return urlparse(url).netloc.lower() in {"fere.ee", "www.fere.ee"}


def is_category_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    lower_path = path.lower()

    if not is_internal_url(url):
        return False

    if lower_path in IGNORED_EXACT_PATHS:
        return False

    if any(part in lower_path for part in IGNORED_PATH_PARTS):
        return False

    if lower_path.endswith(
        (
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
            ".svg",
            ".css",
            ".js",
            ".xml",
            ".pdf",
            ".zip",
        )
    ):
        return False

    return lower_path.endswith(".html")


# ============================================================
# HTTP-КЛИЕНТ
# ============================================================

class HttpClient:
    def __init__(self) -> None:
        self.semaphore = asyncio.Semaphore(CONCURRENCY)
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "HttpClient":
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

        connector = aiohttp.TCPConnector(
            limit=CONCURRENCY,
            limit_per_host=CONCURRENCY,
            ttl_dns_cache=300,
        )

        self.session = aiohttp.ClientSession(
            headers=HEADERS,
            timeout=timeout,
            connector=connector,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.session is not None:
            await self.session.close()

    async def get_text(self, url: str) -> str:
        if self.session is None:
            raise RuntimeError("HTTP-клиент не запущен")

        async with self.semaphore:
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    await asyncio.sleep(
                        random.uniform(
                            REQUEST_DELAY_MIN,
                            REQUEST_DELAY_MAX,
                        )
                    )

                    async with self.session.get(
                        url,
                        allow_redirects=True,
                    ) as response:
                        if response.status == 200:
                            return await response.text(errors="replace")

                        if response.status in {
                            408,
                            425,
                            429,
                            500,
                            502,
                            503,
                            504,
                        }:
                            retry_after = response.headers.get("Retry-After")

                            try:
                                delay = float(retry_after) if retry_after else 0
                            except (TypeError, ValueError):
                                delay = 0

                            if delay <= 0:
                                delay = (
                                    RETRY_BASE_DELAY * (2 ** (attempt - 1))
                                    + random.uniform(0.2, 1.0)
                                )

                            print(
                                f"HTTP {response.status}: {url}\n"
                                f"Повтор {attempt}/{MAX_RETRIES} "
                                f"через {delay:.1f} сек."
                            )
                            await asyncio.sleep(delay)
                            continue

                        body = await response.text(errors="replace")
                        raise RuntimeError(
                            f"HTTP {response.status}: {body[:300]}"
                        )

                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    RuntimeError,
                ) as error:
                    if attempt == MAX_RETRIES:
                        raise RuntimeError(
                            f"Не удалось загрузить {url}: {error}"
                        ) from error

                    delay = (
                        RETRY_BASE_DELAY * (2 ** (attempt - 1))
                        + random.uniform(0.2, 1.0)
                    )

                    print(
                        f"Ошибка запроса: {error}\n"
                        f"Повтор {attempt}/{MAX_RETRIES} "
                        f"через {delay:.1f} сек."
                    )
                    await asyncio.sleep(delay)

        raise RuntimeError(f"Не удалось загрузить {url}")


# ============================================================
# КАТЕГОРИИ И ПАГИНАЦИЯ
# ============================================================

def extract_category_links(home_html: str) -> list[str]:
    soup = BeautifulSoup(home_html, "html.parser")
    found: set[str] = set()

    for anchor in soup.select("a[href]"):
        url = normalize_url(anchor.get("href"))

        if url and is_category_url(url):
            found.add(set_page(url, 1))

    return sorted(found)


def get_total_pages(soup: BeautifulSoup) -> int:
    pages = [1]

    for anchor in soup.select(".pages a[href], .pager a[href]"):
        href = anchor.get("href")

        if not href:
            continue

        query = dict(parse_qsl(urlparse(html.unescape(href)).query))
        page_value = query.get("p")

        if page_value and page_value.isdigit():
            pages.append(int(page_value))

    next_link = soup.select_one(
        ".pages a.next[href], .pager a.next[href], a.next.i-next[href]"
    )

    if next_link:
        query = dict(
            parse_qsl(
                urlparse(html.unescape(str(next_link.get("href")))).query
            )
        )
        page_value = query.get("p")
        if page_value and page_value.isdigit():
            pages.append(int(page_value))

    amount = text_of(soup.select_one(".pager .amount"))

    if amount:
        numbers = [int(value) for value in re.findall(r"\d+", amount)]

        if numbers:
            total_products = max(numbers)
            first_page_count = len(
                soup.select(
                    "ol.products-list > li.item, "
                    "ol.products-list li.item"
                )
            )

            if first_page_count > 0:
                calculated = (
                    total_products + first_page_count - 1
                ) // first_page_count
                pages.append(calculated)

    return max(pages)


# ============================================================
# РАЗБОР ТОВАРА
# ============================================================

def extract_product_link(item: Tag) -> str:
    selectors = (
        "h2.product-name a[href]",
        ".product-name a[href]",
        "a.product-image[href]",
        ".product-pictures a[href]",
        "a[href*='/catalog/product/view/']",
        "a[href]",
    )

    for selector in selectors:
        for anchor in item.select(selector):
            href = anchor.get("href")

            if not href:
                continue

            url = normalize_url(href)

            if not url or not is_internal_url(url):
                continue

            lower_url = url.lower()

            if any(
                marker in lower_url
                for marker in (
                    "/checkout/",
                    "/customer/",
                    "/wishlist/",
                    "/review/",
                    "/sendfriend/",
                    "/media/",
                )
            ):
                continue

            if lower_url.endswith(
                (
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".gif",
                    ".webp",
                    ".svg",
                )
            ):
                continue

            return url

    return ""


def extract_image(item: Tag) -> str:
    anchor = item.select_one(
        ".product-pictures a.product-image[href], "
        "a.product-image[href]"
    )

    if anchor is not None:
        image_url = normalize_url(anchor.get("href"))

        if image_url and not any(
            marker in image_url.lower()
            for marker in PLACEHOLDER_MARKERS
        ):
            return image_url

    selectors = (
        ".product-pictures img:not([src*='/brands/'])",
        "img.product-image",
        ".product-image img",
        "img[src]",
    )

    for selector in selectors:
        image = item.select_one(selector)

        if image is None:
            continue

        src = (
            image.get("data-src")
            or image.get("data-original")
            or image.get("src")
        )

        image_url = normalize_url(src)

        if image_url and not any(
            marker in image_url.lower()
            for marker in PLACEHOLDER_MARKERS
        ):
            return image_url

    return ""


def extract_prices(item: Tag) -> tuple[float | str, float | str]:
    regular_selectors = (
        ".price-box .old-price .price",
        ".price-box .regular-price .price",
        ".price-box .price",
    )

    sale_selectors = (
        ".price-box .discountedprice .price",
        ".price-box .special-price .price",
        ".price-box .final-price .price",
    )

    regular_price: float | str = ""
    sale_price: float | str = ""

    for selector in regular_selectors:
        element = item.select_one(selector)

        if element is not None:
            parsed = parse_price(text_of(element))
            if parsed != "":
                regular_price = parsed
                break

    for selector in sale_selectors:
        element = item.select_one(selector)

        if element is not None:
            parsed = parse_price(text_of(element))
            if parsed != "":
                sale_price = parsed
                break

    # Когда на странице указана только одна цена, считаем её обычной.
    if regular_price == "" and sale_price != "":
        regular_price = sale_price
        sale_price = ""

    # Если обычная и скидочная цена совпадают, скидки фактически нет.
    if regular_price != "" and sale_price == regular_price:
        sale_price = ""

    return regular_price, sale_price


def parse_product_item(item: Tag, category_url: str) -> dict[str, Any]:
    name_tag = (
        item.select_one("h2.product-name")
        or item.select_one(".product-name")
        or item.select_one("h3.product-name")
    )
    name = text_of(name_tag)

    sku_tag = (
        item.select_one("strong.product-code")
        or item.select_one(".product-code")
        or item.select_one("[class*='product-code']")
    )
    sku = normalize_sku(text_of(sku_tag))

    ean_tag = (
        item.select_one(".product-ean")
        or item.select_one("[class*='product-ean']")
        or item.select_one("[class*='ean']")
    )
    ean = normalize_ean(text_of(ean_tag))

    regular_price, sale_price = extract_prices(item)

    return {
        "Название товара": name,
        "Цена": regular_price,
        "Цена со скидкой": sale_price,
        "Цена со скидкой 2": "",
        "Штрихкод": ean,
        "Код магазина": sku,
        "Фото": extract_image(item),
        "Ссылка": category_url,
    }


def parse_category_page(
    page_html: str,
    category_url: str,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(page_html, "html.parser")

    items = soup.select(
        "ol.products-list > li.item, "
        "ol.products-list li.item"
    )

    return [
        parse_product_item(item, category_url)
        for item in items
    ]


# ============================================================
# ЗАГРУЗКА КАТЕГОРИЙ
# ============================================================

async def scrape_category(
    client: HttpClient,
    category_url: str,
) -> list[dict[str, Any]]:
    first_url = set_page(category_url, 1)
    first_html = await client.get_text(first_url)

    soup = BeautifulSoup(first_html, "html.parser")
    first_products = parse_category_page(first_html, first_url)

    if not first_products:
        return []

    total_pages = get_total_pages(soup)
    all_products = list(first_products)

    if total_pages <= 1:
        return all_products

    tasks = {
        page: asyncio.create_task(
            client.get_text(set_page(category_url, page))
        )
        for page in range(2, total_pages + 1)
    }

    for page, task in tasks.items():
        try:
            page_html = await task
            products = parse_category_page(
                page_html,
                set_page(category_url, page),
            )
            all_products.extend(products)
        except Exception as error:
            print(
                f"Ошибка страницы {page} категории {category_url}: {error}"
            )

    return all_products


# ============================================================
# ДЕДУПЛИКАЦИЯ
# ============================================================

def row_completeness(row: dict[str, Any]) -> int:
    return sum(
        value not in (None, "")
        for value in row.values()
    )


def deduplicate_products(
    products: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Товары с одинаковым EAN считаются дублями.
    Без EAN товары сохраняются все.
    """
    unique_by_ean: dict[str, dict[str, Any]] = {}
    without_ean: list[dict[str, Any]] = []

    for product in products:
        ean = normalize_ean(product.get("Штрихкод"))

        if not ean:
            without_ean.append(product)
            continue

        product["Штрихкод"] = ean
        current = unique_by_ean.get(ean)

        if current is None or row_completeness(product) > row_completeness(current):
            unique_by_ean[ean] = product

    return list(unique_by_ean.values()) + without_ean


# ============================================================
# EXCEL
# ============================================================

def save_to_excel(products: list[dict[str, Any]]) -> Path:
    dataframe = pd.DataFrame(products, columns=COLUMNS)

    if not dataframe.empty:
        dataframe = dataframe.sort_values(
            by=["Название товара", "Код магазина"],
            kind="stable",
            na_position="last",
        )

    dataframe.to_excel(
        OUTPUT_FILE,
        index=False,
        engine="openpyxl",
    )

    return OUTPUT_FILE.resolve()


# ============================================================
# ЗАПУСК
# ============================================================

async def _main() -> None:
    print("Запуск парсера Fere.ee")

    async with HttpClient() as client:
        print("Загружаем главную страницу...")
        home_html = await client.get_text(BASE_URL)

        category_urls = extract_category_links(home_html)
        print(f"Найдено категорий: {len(category_urls)}")

        if not category_urls:
            raise RuntimeError(
                "Категории не найдены. Возможно, сайт изменил разметку."
            )

        all_products: list[dict[str, Any]] = []
        processed = 0

        for start in range(0, len(category_urls), CONCURRENCY):
            batch_urls = category_urls[start:start + CONCURRENCY]

            results = await asyncio.gather(
                *[
                    scrape_category(client, url)
                    for url in batch_urls
                ],
                return_exceptions=True,
            )

            for url, result in zip(batch_urls, results):
                processed += 1

                if isinstance(result, Exception):
                    print(f"Ошибка категории {url}: {result}")
                else:
                    all_products.extend(result)

                print(
                    f"Категории: {processed}/{len(category_urls)} | "
                    f"строк: {len(all_products)}"
                )

    unique_products = deduplicate_products(all_products)

    print(f"Собрано строк: {len(all_products)}")
    print(f"После удаления дублей по EAN: {len(unique_products)}")

    if not unique_products:
        raise RuntimeError(
            "Товары не найдены. Возможно, сайт изменил HTML-разметку."
        )

    output_path = save_to_excel(unique_products)
    print(f"Готово: {output_path}")


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
        print("Остановлено пользователем")
    except Exception as error:
        print(f"Критическая ошибка: {error}")
        raise
