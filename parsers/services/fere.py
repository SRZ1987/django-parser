import asyncio
import html
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .store_catalog import (
    AsyncStoreClient,
    StoreCatalogParser,
    StoreCategory,
    StoreProduct,
    absolute_url,
    choose_price_pair,
    clean_barcode,
    clean_text,
    stable_external_id,
)


FERE_WEBSITE_URL = "https://fere.ee/"
FERE_CONCURRENCY = 4
NUMBER_RE = re.compile(r"\d+")
HREF_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"']", re.I)
LI_RE = re.compile(r"<li\b[^>]*class=[\"'][^\"']*\bitem\b[^\"']*[\"'][^>]*>(.*?)</li>", re.I | re.S)
PRICE_RE = re.compile(r"<[^>]*class=[\"'][^\"']*(?:old-price|regular-price|special-price|final-price|discountedprice|price)[^\"']*[\"'][^>]*>(.*?)</[^>]+>", re.I | re.S)


class FereClient(AsyncStoreClient):
    base_url = FERE_WEBSITE_URL
    timeout_seconds = 45
    max_retries = 5
    concurrency = FERE_CONCURRENCY
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; django-parser/1.0; +https://github.com/SRZ1987/django-parser)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "et-EE,et;q=0.9,en;q=0.8,ru;q=0.7",
        "Referer": FERE_WEBSITE_URL,
        "Cache-Control": "no-cache",
    }

    async def fetch_products(self):
        home_html = await self.request_text("GET", FERE_WEBSITE_URL)
        category_urls = extract_category_links(home_html)
        if not category_urls:
            raise ValueError("FERE categories were not found.")

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
                await self.log(f"FERE progress: categories={index}/{len(categories)}, products={len(products)}")
        return categories, products, True

    async def fetch_category(self, category):
        first_url = set_page(category.url, 1)
        first_html = await self.request_text("GET", first_url)
        products = parse_category_page(first_html, category)
        total_pages = get_total_pages(first_html)
        for page in range(2, total_pages + 1):
            page_html = await self.request_text("GET", set_page(category.url, page))
            products.extend(parse_category_page(page_html, category))
        return products


def normalize_url(url, base_url=FERE_WEBSITE_URL):
    absolute = absolute_url(url, base_url)
    parsed = urlparse(absolute)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"___store", "___from_store", "dir", "order", "mode", "limit", "hidepictures"}
    ]
    return urlunparse((parsed.scheme.lower() or "https", parsed.netloc.lower(), parsed.path or "/", "", urlencode(query), ""))


def set_page(url, page):
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if page <= 1:
        query.pop("p", None)
    else:
        query["p"] = str(page)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), ""))


def is_category_url(url):
    parsed = urlparse(url)
    path = parsed.path.lower().rstrip("/")
    if parsed.netloc.lower() not in {"fere.ee", "www.fere.ee"}:
        return False
    if not path.endswith(".html"):
        return False
    return not any(part in path for part in ("/customer/", "/checkout/", "/catalogsearch/", "/contacts/", "/blog/", "/media/"))


def extract_category_links(home_html):
    return sorted({set_page(normalize_url(match), 1) for match in HREF_RE.findall(home_html) if is_category_url(normalize_url(match))})


def category_name_from_url(url):
    slug = urlparse(url).path.rsplit("/", 1)[-1].replace(".html", "")
    return clean_text(slug.replace("-", " ").title()) or "FERE"


def get_total_pages(page_html):
    pages = [1]
    for match in re.findall(r"[?&]p=(\d+)", html.unescape(page_html)):
        pages.append(int(match))
    amount_text = clean_text(re.sub(r"<[^>]+>", " ", page_html))
    numbers = [int(value) for value in NUMBER_RE.findall(amount_text)]
    if numbers and len(LI_RE.findall(page_html)) > 0:
        total_products = max(numbers)
        per_page = len(LI_RE.findall(page_html))
        if total_products < 100000:
            pages.append(max(1, (total_products + per_page - 1) // per_page))
    return min(max(pages), 500)


def first_match(pattern, text, default=""):
    match = re.search(pattern, text, re.I | re.S)
    return clean_text(match.group(1)) if match else default


def attr_value(fragment, tag, attr):
    match = re.search(rf"<{tag}\b[^>]*\b{attr}=[\"']([^\"']+)[\"']", fragment, re.I | re.S)
    return html.unescape(match.group(1)) if match else ""


def parse_category_page(page_html, category):
    return [parse_product_fragment(fragment, category) for fragment in LI_RE.findall(page_html)]


def parse_product_fragment(fragment, category):
    name = first_match(r"<h[23]\b[^>]*class=[\"'][^\"']*product-name[^\"']*[\"'][^>]*>(.*?)</h[23]>", fragment)
    if not name:
        name = first_match(r"class=[\"'][^\"']*product-name[^\"']*[\"'][^>]*>(.*?)</", fragment)
    sku = clean_text(re.sub(r"^(?:Tootekood|Product code|Artikkel)\s*:?\s*", "", first_match(r"class=[\"'][^\"']*product-code[^\"']*[\"'][^>]*>(.*?)</", fragment), flags=re.I))
    barcode_text = first_match(r"class=[\"'][^\"']*(?:product-ean|ean)[^\"']*[\"'][^>]*>(.*?)</", fragment)
    barcode = clean_barcode(re.sub(r"^(?:EAN|GTIN|Barcode|Ribakood)\s*:?\s*", "", barcode_text, flags=re.I))
    product_url = normalize_url(attr_value(fragment, "a", "href"))
    image_url = normalize_url(attr_value(fragment, "img", "data-src") or attr_value(fragment, "img", "data-original") or attr_value(fragment, "img", "src"))
    price_chunks = [clean_text(re.sub(r"<[^>]+>", " ", chunk)) for chunk in PRICE_RE.findall(fragment)]
    regular = price_chunks[0] if price_chunks else None
    sale = price_chunks[-1] if len(price_chunks) > 1 else None
    price, sale_price = choose_price_pair(regular, sale)
    external_id = sku or barcode or stable_external_id(product_url)
    return StoreProduct(
        external_id=external_id,
        sku=sku,
        barcode=barcode,
        name=name,
        price=price,
        sale_price=sale_price,
        product_url=product_url,
        image_url=image_url,
        category_external_id=category.external_id,
        category_name=category.name,
        category_url=category.url,
        is_available=True,
    )


class FereParser(StoreCatalogParser):
    code = "fere"
    shop_name = "FERE"
    website_url = FERE_WEBSITE_URL

    async def _fetch_remote_data(self):
        async def live_log(message):
            print(message, flush=True)
            await asyncio.to_thread(self.log, message)

        async with FereClient(log_callback=live_log) as client:
            return await client.fetch_products()
