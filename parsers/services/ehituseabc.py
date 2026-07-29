import asyncio

from .store_catalog import (
    AsyncStoreClient,
    StoreCatalogParser,
    StoreProduct,
    absolute_url,
    choose_price_pair,
    clean_barcode,
    clean_text,
)


EHITUSEABC_WEBSITE_URL = "https://www.ehituseabc.ee"
KLEVU_URL = "https://eucs32v2.ksearchnet.com/cs/v2/search"
KLEVU_API_KEY = "klevu-168180264665813326"
PAGE_SIZE = 100


class EhituseABCClient(AsyncStoreClient):
    base_url = EHITUSEABC_WEBSITE_URL
    timeout_seconds = 60
    max_retries = 5
    concurrency = 5
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": EHITUSEABC_WEBSITE_URL,
        "Referer": f"{EHITUSEABC_WEBSITE_URL}/",
        "User-Agent": "Mozilla/5.0 (compatible; django-parser/1.0; +https://github.com/SRZ1987/django-parser)",
    }

    async def fetch_products(self):
        first_data = await self.fetch_page(0, PAGE_SIZE)
        products = extract_records(first_data)
        total = extract_total(first_data)
        if total <= 0:
            return [], [], True

        offsets = list(range(PAGE_SIZE, total, PAGE_SIZE))
        for index, offset in enumerate(offsets, start=1):
            limit = min(PAGE_SIZE, total - offset)
            data = await self.fetch_page(offset, limit)
            products.extend(extract_records(data))
            if index % 10 == 0 or offset + limit >= total:
                await self.log(f"EHITUSEABC progress: records={len(products)}/{total}")

        normalized = [normalize_record(record) for record in products[:total]]
        return [], [product for product in normalized if product.external_id], True

    async def fetch_page(self, offset, limit):
        return await self.request_json("POST", KLEVU_URL, json=build_payload(offset, limit))


def first_value(record, *keys):
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def build_payload(offset, limit):
    return {
        "context": {"apiKeys": [KLEVU_API_KEY]},
        "recordQueries": [
            {
                "id": "productSearch",
                "typeOfRequest": "SEARCH",
                "settings": {
                    "query": {"term": "*"},
                    "typeOfRecords": ["KLEVU_PRODUCT"],
                    "limit": limit,
                    "offset": offset,
                },
            }
        ],
    }


def query_result(data):
    results = data.get("queryResults")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise ValueError("Klevu response does not contain queryResults.")
    return results[0]


def extract_records(data):
    records = query_result(data).get("records", [])
    return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []


def extract_total(data):
    result = query_result(data)
    meta = result.get("meta", {})
    candidates = [
        result.get("totalResultsFound"),
        result.get("totalResults"),
        meta.get("totalResultsFound") if isinstance(meta, dict) else None,
        meta.get("totalResults") if isinstance(meta, dict) else None,
        meta.get("totalRecords") if isinstance(meta, dict) else None,
    ]
    for value in candidates:
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return len(extract_records(data))


def normalize_record(record):
    sku = clean_text(first_value(record, "sku", "itemGroupId", "productCode", "product_code", "id"))
    regular_price, sale_price = choose_price_pair(
        first_value(record, "price", "regularPrice", "regular_price", "originalPrice", "original_price", "basePrice"),
        first_value(record, "salePrice", "sale_price", "specialPrice", "special_price", "discountPrice", "discount_price", "finalPrice"),
    )
    return StoreProduct(
        external_id=sku or clean_text(first_value(record, "id", "itemGroupId")),
        sku=sku,
        barcode=clean_barcode(first_value(record, "barcode", "ean", "EAN", "gtin", "GTIN", "upc", "UPC")),
        name=clean_text(first_value(record, "name", "title", "productName")),
        price=regular_price,
        sale_price=sale_price,
        product_url=absolute_url(first_value(record, "url", "productUrl", "product_url", "link"), EHITUSEABC_WEBSITE_URL),
        image_url=absolute_url(first_value(record, "image", "imageUrl", "image_url", "smallImage", "small_image", "thumbnail"), EHITUSEABC_WEBSITE_URL),
        is_available=True,
    )


class EhituseABCParser(StoreCatalogParser):
    code = "ehituseabc"
    shop_name = "Ehituse ABC"
    website_url = EHITUSEABC_WEBSITE_URL

    async def _fetch_remote_data(self):
        async def live_log(message):
            print(message, flush=True)
            await asyncio.to_thread(self.log, message)

        async with EhituseABCClient(log_callback=live_log) as client:
            return await client.fetch_products()
