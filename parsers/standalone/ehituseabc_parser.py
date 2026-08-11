import asyncio
import contextlib
import io
import json
import random
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp
import pandas as pd


BASE_URL = "https://www.ehituseabc.ee"
KLEVU_URL = "https://eucs32v2.ksearchnet.com/cs/v2/search"
KLEVU_API_KEY = "klevu-168180264665813326"

OUTPUT_FILE = Path("ehituseabc.xlsx")

PAGE_SIZE = 100
CONCURRENCY = 5
REQUEST_TIMEOUT = 60
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


def first_value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)

        if value not in (None, ""):
            return value

    return None


def clean_text(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, list):
        value = " ".join(
            str(item)
            for item in value
            if item is not None
        )

    text = re.sub(r"<[^>]+>", " ", str(value))
    return re.sub(r"\s+", " ", text).strip()


def clean_price(value: Any) -> float | str:
    if value in (None, "") or isinstance(value, bool):
        return ""

    if isinstance(value, (int, float)):
        return round(float(value), 2)

    text = (
        str(value)
        .replace("\xa0", "")
        .replace(" ", "")
        .replace(",", ".")
    )

    match = re.search(r"-?\d+(?:\.\d+)?", text)

    if not match:
        return ""

    try:
        price = round(float(match.group()), 2)
        return price if price > 0 else ""
    except ValueError:
        return ""


def clean_barcode(value: Any) -> str:
    barcode = clean_text(value)

    if re.fullmatch(r"\d+\.0", barcode):
        barcode = barcode[:-2]

    return re.sub(r"[\s\u00A0\-]+", "", barcode)


def absolute_url(value: Any) -> str:
    url = clean_text(value)

    if not url:
        return ""

    if url.startswith("//"):
        return f"https:{url}"

    return urljoin(BASE_URL, url)


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    code = first_value(
        record,
        "sku",
        "itemGroupId",
        "productCode",
        "product_code",
        "id",
    )

    name = first_value(
        record,
        "name",
        "title",
        "productName",
    )

    regular_price = clean_price(
        first_value(
            record,
            "price",
            "regularPrice",
            "regular_price",
            "originalPrice",
            "original_price",
            "basePrice",
        )
    )

    sale_price = clean_price(
        first_value(
            record,
            "salePrice",
            "sale_price",
            "specialPrice",
            "special_price",
            "discountPrice",
            "discount_price",
            "finalPrice",
        )
    )

    if (
        regular_price != ""
        and sale_price != ""
        and float(sale_price) >= float(regular_price)
    ):
        sale_price = ""

    barcode = first_value(
        record,
        "barcode",
        "ean",
        "EAN",
        "gtin",
        "GTIN",
        "upc",
        "UPC",
    )

    image = first_value(
        record,
        "image",
        "imageUrl",
        "image_url",
        "smallImage",
        "small_image",
        "thumbnail",
    )

    product_url = first_value(
        record,
        "url",
        "productUrl",
        "product_url",
        "link",
    )
    category_name = clean_text(
        first_value(record, "category", "categoryName", "category_name", "klevu_category")
    )
    category_id = clean_text(first_value(record, "categoryId", "category_id"))
    description = clean_text(
        first_value(record, "shortDesc", "shortDescription", "description", "summary")
    )

    return {
        "Название товара": clean_text(name),
        "Цена": regular_price,
        "Цена со скидкой": sale_price,
        "Цена со скидкой 2": "",
        "Штрихкод": clean_barcode(barcode),
        "Код магазина": clean_text(code),
        "Фото": absolute_url(image),
        "Ссылка": absolute_url(product_url),
        "SKU": clean_text(first_value(record, "sku", "productCode", "product_code")),
        "Category": category_name,
        "Category ID": category_id,
        "Description": description,
        "Brand": clean_text(first_value(record, "brand", "manufacturer", "vendor")),
        "Model": clean_text(first_value(record, "model", "itemGroupId", "mpn")),
    }


def build_payload(offset: int, limit: int) -> dict[str, Any]:
    return {
        "context": {
            "apiKeys": [KLEVU_API_KEY],
        },
        "recordQueries": [
            {
                "id": "productSearch",
                "typeOfRequest": "SEARCH",
                "settings": {
                    "query": {
                        "term": "*",
                    },
                    "typeOfRecords": [
                        "KLEVU_PRODUCT",
                    ],
                    "limit": limit,
                    "offset": offset,
                },
            }
        ],
    }


async def fetch_page(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    payload = build_payload(offset, limit)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with semaphore:
                async with session.post(
                    KLEVU_URL,
                    json=payload,
                ) as response:
                    if response.status == 200:
                        return await response.json(
                            content_type=None
                        )

                    text = await response.text()

                    if response.status in {
                        408,
                        425,
                        429,
                        500,
                        502,
                        503,
                        504,
                    }:
                        delay = min(
                            60,
                            2 ** attempt
                            + random.uniform(0.5, 2.5),
                        )

                        print(
                            f"HTTP {response.status}, "
                            f"offset={offset}. "
                            f"Повтор через {delay:.1f} сек."
                        )

                        await asyncio.sleep(delay)
                        continue

                    raise RuntimeError(
                        f"HTTP {response.status}: "
                        f"{text[:500]}"
                    )

        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            RuntimeError,
            json.JSONDecodeError,
        ) as error:
            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    f"Не удалось загрузить offset={offset}: "
                    f"{error}"
                ) from error

            delay = min(
                30,
                2 ** attempt
                + random.uniform(0.5, 2),
            )

            print(
                f"Ошибка offset={offset}: {error}. "
                f"Повтор через {delay:.1f} сек."
            )

            await asyncio.sleep(delay)

    raise RuntimeError(
        f"Не удалось загрузить offset={offset}"
    )


def get_query_result(
    data: dict[str, Any],
) -> dict[str, Any]:
    results = data.get("queryResults")

    if not isinstance(results, list) or not results:
        raise ValueError(
            "В ответе Klevu отсутствует queryResults"
        )

    result = results[0]

    if not isinstance(result, dict):
        raise ValueError(
            "Некорректный формат queryResults"
        )

    return result


def extract_records(
    data: dict[str, Any],
) -> list[dict[str, Any]]:
    records = get_query_result(data).get(
        "records",
        [],
    )

    if not isinstance(records, list):
        return []

    return [
        record
        for record in records
        if isinstance(record, dict)
    ]


def extract_total(data: dict[str, Any]) -> int:
    result = get_query_result(data)
    meta = result.get("meta", {})

    values = [
        result.get("totalResultsFound"),
        result.get("totalResults"),
        meta.get("totalResultsFound")
        if isinstance(meta, dict)
        else None,
        meta.get("totalResults")
        if isinstance(meta, dict)
        else None,
        meta.get("totalRecords")
        if isinstance(meta, dict)
        else None,
    ]

    for value in values:
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass

    return len(extract_records(data))


async def download_all_products() -> list[dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(
        total=REQUEST_TIMEOUT
    )

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/150.0.0.0 Safari/537.36"
        ),
    }

    semaphore = asyncio.Semaphore(CONCURRENCY)

    connector = aiohttp.TCPConnector(
        limit=CONCURRENCY,
        ssl=False,
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
        connector=connector,
    ) as session:
        first_data = await fetch_page(
            session,
            semaphore,
            offset=0,
            limit=PAGE_SIZE,
        )

        products = extract_records(first_data)
        total = extract_total(first_data)

        print(f"Найдено товаров: {total}")

        offsets = list(
            range(PAGE_SIZE, total, PAGE_SIZE)
        )

        async def load_offset(
            offset: int,
        ) -> tuple[int, list[dict[str, Any]]]:
            limit = min(PAGE_SIZE, total - offset)

            data = await fetch_page(
                session,
                semaphore,
                offset=offset,
                limit=limit,
            )

            records = extract_records(data)

            print(
                f"Загружена страница offset={offset}, "
                f"товаров: {len(records)}"
            )

            return offset, records

        results = await asyncio.gather(
            *(
                load_offset(offset)
                for offset in offsets
            )
        )

        results.sort(key=lambda item: item[0])

        for _, records in results:
            products.extend(records)

        return products[:total]


def save_excel(
    products: list[dict[str, Any]],
) -> None:
    rows = [
        normalize_record(record)
        for record in products
    ]

    dataframe = pd.DataFrame(
        rows,
        columns=COLUMNS,
    )

    if dataframe.empty:
        raise ValueError(
            "Нет товаров для сохранения"
        )

    dataframe = dataframe.fillna("")

    dataframe = dataframe[
        (
            dataframe["Код магазина"] != ""
        )
        | (
            dataframe["Название товара"] != ""
        )
        | (
            dataframe["Ссылка"] != ""
        )
    ]

    with_code = dataframe[
        dataframe["Код магазина"] != ""
    ].drop_duplicates(
        subset=["Код магазина"],
        keep="first",
    )

    without_code = dataframe[
        dataframe["Код магазина"] == ""
    ].drop_duplicates(
        subset=["Ссылка", "Название товара"],
        keep="first",
    )

    dataframe = pd.concat(
        [with_code, without_code],
        ignore_index=True,
    )

    dataframe = dataframe.sort_values(
        by=[
            "Название товара",
            "Код магазина",
        ],
        na_position="last",
    )

    with pd.ExcelWriter(
        OUTPUT_FILE,
        engine="openpyxl",
    ) as writer:
        dataframe.to_excel(
            writer,
            sheet_name="Товары",
            index=False,
        )

        worksheet = writer.sheets["Товары"]
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = (
            worksheet.dimensions
        )

        widths = {
            "A": 65,
            "B": 14,
            "C": 18,
            "D": 20,
            "E": 22,
            "F": 20,
            "G": 70,
            "H": 80,
        }

        for column, width in widths.items():
            worksheet.column_dimensions[
                column
            ].width = width

        for row_number in range(
            2,
            worksheet.max_row + 1,
        ):
            worksheet[
                f"E{row_number}"
            ].number_format = "@"

            worksheet[
                f"F{row_number}"
            ].number_format = "@"

            for column in ("B", "C", "D"):
                worksheet[
                    f"{column}{row_number}"
                ].number_format = "0.00"

    print(
        f"Готово: {OUTPUT_FILE.resolve()} | "
        f"товаров: {len(dataframe)}"
    )


async def _main() -> None:
    products = await download_all_products()

    if not products:
        raise RuntimeError(
            "Klevu не вернул товары"
        )

    await asyncio.to_thread(
        save_excel,
        products,
    )


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
        print(
            f"\nКритическая ошибка: "
            f"{type(error).__name__}: {error}"
        )
        raise
