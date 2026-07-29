import html
import json
import re
from urllib.parse import urlparse

from .store_catalog import (
    AsyncStoreClient,
    StoreCatalogParser,
    StoreProduct,
    absolute_url,
    choose_price_pair,
    clean_barcode,
    clean_text,
    nested_get,
)


BAUHOF_WEBSITE_URL = "https://www.bauhof.ee"
BAUHOF_SITEMAP_INDEX_URL = f"{BAUHOF_WEBSITE_URL}/sitemap.xml"
BAUHOF_PRODUCT_API_URL = f"{BAUHOF_WEBSITE_URL}/api/magento/products?locale=et"
BAUHOF_BATCH_SIZE = 100


class BauhofClient(AsyncStoreClient):
    base_url = BAUHOF_WEBSITE_URL
    timeout_seconds = 120
    max_retries = 5
    concurrency = 8
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; django-parser/1.0; +https://github.com/SRZ1987/django-parser)",
        "Accept-Language": "et-EE,et;q=0.9,en;q=0.8,ru;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    async def fetch_products(self):
        sku_map = await self.fetch_sku_map()
        products = []
        for start in range(0, len(sku_map), BAUHOF_BATCH_SIZE):
            batch = sorted(sku_map)[start : start + BAUHOF_BATCH_SIZE]
            data = await self.fetch_product_batch(batch)
            for item in extract_items(data):
                product = normalize_product(item, sku_map.get(clean_text(item.get("sku")), ""))
                if product.external_id:
                    products.append(product)
            await self.log(f"BAUHOF progress: batches={start // BAUHOF_BATCH_SIZE + 1}, products={len(products)}")
        return [], products, True

    async def fetch_sku_map(self):
        index_xml = await self.request_text("GET", BAUHOF_SITEMAP_INDEX_URL, headers={"Accept": "application/xml,text/xml,*/*"})
        sitemap_urls = sorted(
            url
            for url in extract_locations(index_xml)
            if "/sitemaps/products-et/" in url.lower() and url.lower().endswith(".xml")
        )
        if not sitemap_urls:
            raise ValueError("BAUHOF product sitemaps were not found.")

        sku_map = {}
        for index, sitemap_url in enumerate(sitemap_urls, start=1):
            xml_text = await self.request_text("GET", sitemap_url, headers={"Accept": "application/xml,text/xml,*/*"})
            for product_url in extract_locations(xml_text):
                product = extract_product_from_url(product_url)
                if product:
                    sku, clean_url = product
                    sku_map[sku] = clean_url
            if index % 5 == 0 or index == len(sitemap_urls):
                await self.log(f"BAUHOF sitemap progress: {index}/{len(sitemap_urls)}, sku={len(sku_map)}")
        return sku_map

    async def fetch_product_batch(self, skus):
        payload = [{"filter": {"sku": {"in": skus}}, "pageSize": len(skus), "currentPage": 1}]
        text = await self.request_text(
            "POST",
            BAUHOF_PRODUCT_API_URL,
            json=payload,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": BAUHOF_WEBSITE_URL,
                "Referer": f"{BAUHOF_WEBSITE_URL}/",
            },
        )
        return json.loads(text)


def extract_locations(xml_text):
    return [html.unescape(match).strip() for match in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml_text, re.I | re.S)]


def extract_product_from_url(url):
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    match = re.match(r"^/et/p/([^/]+)(?:/.*)?$", path, re.I)
    if not match:
        return None
    sku = clean_text(match.group(1))
    return (sku, f"{parsed.scheme}://{parsed.netloc}{path}") if sku else None


def extract_items(data):
    if isinstance(data, list):
        items = []
        for item in data:
            items.extend(extract_items(item))
        return items
    raw_items = nested_get(data, "data", "products", "items", default=[])
    return [item for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []


def item_price(item, price_type):
    return nested_get(item, "price_range", "minimum_price", price_type, "value") or nested_get(
        item,
        "price_range",
        "maximum_price",
        price_type,
        "value",
    )


def normalize_product(item, sitemap_url=""):
    sku = clean_text(item.get("sku"))
    regular_price, sale_price = choose_price_pair(item_price(item, "regular_price"), item_price(item, "final_price"))
    product_url = sitemap_url
    if not product_url and sku:
        product_url = f"{BAUHOF_WEBSITE_URL}/et/p/{sku}/{clean_text(item.get('url_key'))}".rstrip("/")
    return StoreProduct(
        external_id=sku,
        sku=sku,
        barcode=clean_barcode(item.get("barcode")),
        name=clean_text(item.get("name")),
        price=regular_price,
        sale_price=sale_price,
        product_url=absolute_url(product_url, BAUHOF_WEBSITE_URL),
        image_url=absolute_url(nested_get(item, "thumbnail", "url", default=""), BAUHOF_WEBSITE_URL),
        is_available=True,
    )


class BauhofParser(StoreCatalogParser):
    code = "bauhof"
    shop_name = "Bauhof"
    website_url = BAUHOF_WEBSITE_URL

    async def _fetch_remote_data(self):
        async def live_log(message):
            print(message, flush=True)
            await self.log_async(message)

        async with BauhofClient(log_callback=live_log) as client:
            return await client.fetch_products()

    async def log_async(self, message):
        await __import__("asyncio").to_thread(self.log, message)
