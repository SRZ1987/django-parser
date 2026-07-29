import asyncio
import html
import json
import math
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from .store_catalog import (
    AsyncStoreClient,
    StoreCatalogParser,
    StoreCategory,
    StoreProduct,
    absolute_url,
    choose_price_pair,
    clean_barcode,
    clean_text,
    nested_get,
    stable_external_id,
)


BAUHAUS_WEBSITE_URL = "https://www.bauhaus.ee"
PAGE_CONCURRENCY = 2
MAX_PAGES_PER_CATEGORY = 500
TOP_LEVEL_EXCLUDED_SLUGS = {
    "",
    "api",
    "artiklid-ja-napunaiteid",
    "blog",
    "brand",
    "brands",
    "ettevottest",
    "info",
    "kampaaniad",
    "kaubamajad",
    "kaubamargid",
    "kinkekaart",
    "kliendileht",
    "klienditugi",
    "kontakt",
    "login",
    "media",
    "otsing",
    "secure",
    "teenused",
}


class BauhausClient(AsyncStoreClient):
    base_url = BAUHAUS_WEBSITE_URL
    timeout_seconds = 90
    max_retries = 5
    concurrency = PAGE_CONCURRENCY
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "et-EE,et;q=0.9,ru-RU;q=0.8,ru;q=0.7,en;q=0.6",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "referer": f"{BAUHAUS_WEBSITE_URL}/",
        "user-agent": "Mozilla/5.0 (compatible; django-parser/1.0; +https://github.com/SRZ1987/django-parser)",
    }

    async def fetch_products(self):
        home = await self.request_text("GET", BAUHAUS_WEBSITE_URL)
        category_urls = extract_category_urls(home)
        if not category_urls:
            raise ValueError("BAUHAUS categories were not found.")

        categories = [
            StoreCategory(
                external_id=stable_external_id(url),
                name=category_name_from_url(url),
                url=url,
            )
            for url in category_urls
        ]
        products = []
        for index, category in enumerate(categories, start=1):
            products.extend(await self.fetch_category(category))
            if index % 10 == 0 or index == len(categories):
                await self.log(f"BAUHAUS progress: categories={index}/{len(categories)}, products={len(products)}")
        return categories, products, True

    async def fetch_category(self, category):
        first_html = await self.request_text("GET", category.url)
        first_hits = extract_hits_from_document(first_html)
        metadata = extract_catalog_metadata(first_html)
        products = [product_from_hit(hit, category) for hit in first_hits]
        products = [product for product in products if product and product.external_id]
        hits_per_page = metadata["hits_per_page"] or len(first_hits) or 40
        total_hits = metadata["nb_hits"] or len(first_hits)
        expected_pages = min(metadata["nb_pages"] or max(1, math.ceil(total_hits / hits_per_page)), MAX_PAGES_PER_CATEGORY)

        for page in range(2, expected_pages + 1):
            page_html = await self.request_text("GET", add_query_parameter(category.url, "page", page))
            hits = extract_hits_from_document(page_html)
            products.extend(product for product in (product_from_hit(hit, category) for hit in hits) if product and product.external_id)
        return products


def normalize_url(url):
    if not url:
        return ""
    absolute = urljoin(f"{BAUHAUS_WEBSITE_URL}/", html.unescape(clean_text(url)).lstrip("/"))
    parsed = urlparse(absolute)
    if parsed.netloc not in {"bauhaus.ee", "www.bauhaus.ee"}:
        return ""
    path = re.sub(r"/+", "/", parsed.path).rstrip("/") or "/"
    return urlunparse(("https", "www.bauhaus.ee", path, "", "", ""))


def add_query_parameter(url, name, value):
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[name] = str(value)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), parsed.fragment))


def document_versions(text):
    versions = [text]
    decoded = html.unescape(text)
    if decoded != text:
        versions.append(decoded)
    for match in re.finditer(r'self\.__next_f\.push\(\[\d+,\s*("(?:\\.|[^"\\])*")', text, flags=re.S):
        try:
            decoded_push = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(decoded_push, str):
            versions.append(decoded_push)
    expanded = []
    for version in versions:
        expanded.append(version)
        expanded.append(
            version.replace('\\"', '"')
            .replace("\\/", "/")
            .replace("\\u0026", "&")
            .replace("\\u003c", "<")
            .replace("\\u003e", ">")
            .replace("\\u0022", '"')
        )
    return list(dict.fromkeys(item for item in expanded if item))


def json_values_after_marker(text, marker):
    decoder = json.JSONDecoder()
    values = []
    start = 0
    while True:
        position = text.find(marker, start)
        if position == -1:
            break
        value_start = position + len(marker)
        while value_start < len(text) and text[value_start] in " \r\n\t":
            value_start += 1
        try:
            value, _ = decoder.raw_decode(text[value_start:])
            values.append(value)
        except json.JSONDecodeError:
            pass
        start = position + len(marker)
    return values


def extract_hits_from_document(text):
    found = {}
    for version in document_versions(text):
        for value in json_values_after_marker(version, '"hits":'):
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, dict) and clean_text(item.get("sku")) and any(
                    key in item for key in ("name", "url", "canonical_url", "image_url", "grid_image", "price", "bauhaus_price")
                ):
                    found[clean_text(item.get("sku"))] = item
    return list(found.values())


def extract_catalog_metadata(text):
    def ints(field):
        result = []
        for version in document_versions(text):
            for match in re.finditer(rf'"{re.escape(field)}"\s*:\s*(\d+)', version):
                result.append(int(match.group(1)))
        return result

    return {
        "nb_pages": max(ints("nbPages"), default=0),
        "nb_hits": max(ints("nbHits"), default=0),
        "hits_per_page": max(ints("hitsPerPage"), default=0),
    }


def extract_category_urls(document):
    urls = set()
    for version in document_versions(document):
        for match in re.finditer(r'"url_path"\s*:\s*"([^"]+)"', version):
            url_path = clean_text(match.group(1)).strip("/")
            if not url_path or url_path.split("/", 1)[0].lower() in TOP_LEVEL_EXCLUDED_SLUGS:
                continue
            urls.add(normalize_url("/" + url_path))
    if urls:
        return sorted(urls)
    return sorted(
        {
            normalize_url(match)
            for match in re.findall(r"<a\b[^>]*href=[\"']([^\"']+)[\"']", document, re.I)
            if normalize_url(match) and urlparse(normalize_url(match)).path.strip("/")
        }
    )


def category_name_from_url(url):
    slug = urlparse(url).path.strip("/").rsplit("/", 1)[-1]
    return clean_text(slug.replace("-", " ").title()) or "BAUHAUS"


def category_data(hit):
    categories = hit.get("categories")
    if isinstance(categories, dict):
        for key in ("level2", "level1", "level0"):
            value = categories.get(key)
            if isinstance(value, list) and value:
                return clean_text(value[0]).replace(" /// ", " > ")
            if isinstance(value, str):
                return clean_text(value).replace(" /// ", " > ")
    if isinstance(categories, list):
        names = [clean_text(item.get("name")) for item in categories if isinstance(item, dict) and clean_text(item.get("name"))]
        return " > ".join(dict.fromkeys(names))
    return ""


def price_data(hit):
    bauhaus_price = hit.get("bauhaus_price")
    if isinstance(bauhaus_price, dict):
        return (
            nested_get(bauhaus_price, "ordinary_price", "value") or nested_get(bauhaus_price, "regular_price", "value"),
            nested_get(bauhaus_price, "final_price", "value"),
            clean_text(nested_get(bauhaus_price, "final_price", "currency", default="EUR")) or "EUR",
        )
    price = hit.get("price")
    if isinstance(price, dict):
        eur = price.get("EUR")
        if isinstance(eur, dict):
            value = eur.get("group_0", eur.get("default"))
            return value, None, "EUR"
    return None, None, "EUR"


def product_from_hit(hit, source_category):
    sku = clean_text(hit.get("sku"))
    if not sku:
        return None
    product_url = clean_text(hit.get("url") or hit.get("canonical_url") or hit.get("url_key"))
    image_url = clean_text(hit.get("image_url") or hit.get("thumbnail_url") or nested_get(hit, "grid_image", "url", default=""))
    regular, sale, currency = price_data(hit)
    price, sale_price = choose_price_pair(regular, sale)
    category_name = category_data(hit) or source_category.name
    category_external_id = stable_external_id(category_name or source_category.external_id)
    return StoreProduct(
        external_id=sku,
        sku=sku,
        barcode=clean_barcode(hit.get("ean") or hit.get("gtin") or hit.get("barcode")),
        name=clean_text(hit.get("name")),
        brand=clean_text(hit.get("brand_name") or hit.get("brand")),
        price=price,
        sale_price=sale_price,
        currency=currency,
        product_url=absolute_url(product_url, BAUHAUS_WEBSITE_URL),
        image_url=absolute_url(image_url, BAUHAUS_WEBSITE_URL),
        category_external_id=category_external_id,
        category_name=category_name,
        category_url=source_category.url,
        is_available=clean_text(hit.get("stock_status")).lower() not in {"out_of_stock", "sold_out"},
    )


class BauhausParser(StoreCatalogParser):
    code = "bauhaus"
    shop_name = "BAUHAUS"
    website_url = BAUHAUS_WEBSITE_URL

    async def _fetch_remote_data(self):
        async def live_log(message):
            print(message, flush=True)
            await asyncio.to_thread(self.log, message)

        async with BauhausClient(log_callback=live_log) as client:
            return await client.fetch_products()
