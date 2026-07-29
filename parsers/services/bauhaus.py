import asyncio
import html
import json
import math
import os
import random
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from .store_catalog import (
    HttpRequestError,
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

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
except ImportError:  # pragma: no cover - exercised only when dependency is missing.
    CurlAsyncSession = None


BAUHAUS_WEBSITE_URL = "https://www.bauhaus.ee"
PAGE_CONCURRENCY = 2
MAX_PAGES_PER_CATEGORY = 500
EMPTY_PAGE_RETRIES = 3
REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
REQUEST_DELAY_MIN = 0.15
REQUEST_DELAY_MAX = 0.40
RSC_ATTEMPTS = 5

HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "et-EE,et;q=0.9,ru-RU;q=0.8,ru;q=0.7,en;q=0.6",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "referer": f"{BAUHAUS_WEBSITE_URL}/",
    "upgrade-insecure-requests": "1",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
}

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
    "oiguslik",
    "otsing",
    "profimuuk",
    "secure",
    "teenused",
    "tooriistade-laenutus",
}


class BauhausHttpClient:
    def __init__(self, log_callback=None):
        self.log_callback = log_callback
        self.session = None
        self.semaphore = asyncio.Semaphore(PAGE_CONCURRENCY)

    async def __aenter__(self):
        if CurlAsyncSession is None:
            raise RuntimeError("curl_cffi is required for BAUHAUS protected HTML/RSC endpoints.")
        self.session = CurlAsyncSession(headers=HEADERS, impersonate="chrome", max_clients=PAGE_CONCURRENCY)
        await self.log("BAUHAUS transport: curl_cffi impersonate=chrome")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.session is not None:
            await self.session.close()

    async def log(self, message):
        if self.log_callback:
            result = self.log_callback(message)
            if asyncio.iscoroutine(result):
                await result

    async def get_text(self, url, *, headers=None, params=None, endpoint_name="request"):
        if self.session is None:
            raise RuntimeError("BAUHAUS HTTP client is not started.")

        async with self.semaphore:
            last_error = None
            for attempt in range(1, MAX_RETRIES + 1):
                await asyncio.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
                try:
                    response = await self.session.get(
                        url,
                        headers=headers,
                        params=params,
                        timeout=REQUEST_TIMEOUT,
                        allow_redirects=True,
                    )
                    status = response.status_code
                    await self.log(f"BAUHAUS {endpoint_name}: HTTP {status}; url={url}")
                    if status == 200:
                        return response.text

                    retry_after = response.headers.get("Retry-After")
                    if status == 429:
                        await self.log(
                            f"BAUHAUS {endpoint_name}: HTTP 429; Retry-After={retry_after or '-'}; not repeating blocked request."
                        )
                        raise HttpRequestError("BAUHAUS received Vercel 429", status=429, retryable=True)

                    if status in {408, 425, 500, 502, 503, 504}:
                        last_error = HttpRequestError(f"HTTP {status}", status=status, retryable=True)
                        delay = retry_delay(attempt, retry_after)
                        await self.log(f"BAUHAUS {endpoint_name}: retry {attempt}/{MAX_RETRIES} in {delay:.1f}s")
                        await asyncio.sleep(delay)
                        continue

                    raise HttpRequestError(f"HTTP {status}: {response.text[:300]}", status=status)

                except HttpRequestError:
                    raise
                except Exception as exc:
                    last_error = exc
                    if attempt >= MAX_RETRIES:
                        break
                    delay = retry_delay(attempt, None)
                    await self.log(f"BAUHAUS {endpoint_name}: error={exc}; retry {attempt}/{MAX_RETRIES} in {delay:.1f}s")
                    await asyncio.sleep(delay)

            raise HttpRequestError(f"BAUHAUS request failed: {url}. Last error: {last_error}", retryable=True)


def retry_delay(attempt, retry_after):
    try:
        parsed = float(retry_after) if retry_after else 0
    except (TypeError, ValueError):
        parsed = 0
    if parsed > 0:
        return min(parsed, 60)
    return min(60, 2 ** attempt + random.uniform(0.5, 2.0))


class BauhausClient:
    def __init__(self, log_callback=None, category_limit=None):
        self.log_callback = log_callback
        self.category_limit = category_limit

    async def fetch_products(self):
        async with BauhausHttpClient(log_callback=self.log_callback) as http_client:
            try:
                home_document = await http_client.get_text(BAUHAUS_WEBSITE_URL, endpoint_name="homepage")
            except HttpRequestError as exc:
                if exc.status != 429:
                    raise
                await http_client.log("BAUHAUS homepage blocked by 429; trying RSC category source without homepage document.")
                home_document = ""
            tree, source = await discover_category_tree(http_client, home_document)
            root_count, leaf_categories, category_audit = category_urls_from_tree(tree)

            await http_client.log(f"BAUHAUS category source: {source}")
            await http_client.log(f"BAUHAUS root categories: {root_count}")
            await http_client.log(f"BAUHAUS leaf categories: {len(leaf_categories)}")

            if not leaf_categories:
                raise ValueError("BAUHAUS leaf categories were not found.")

            if self.category_limit is not None:
                leaf_categories = leaf_categories[: self.category_limit]
                await http_client.log(f"BAUHAUS category limit applied: {self.category_limit}")

            products = []
            pages_done = 0
            for index, category in enumerate(leaf_categories, start=1):
                await http_client.log(f"BAUHAUS category: {index}/{len(leaf_categories)}; {category.url}")
                category_products, category_pages = await fetch_category_products(http_client, category)
                products.extend(category_products)
                pages_done += category_pages
                await http_client.log(
                    f"BAUHAUS progress: categories={index}/{len(leaf_categories)}; pages={pages_done}; products={len(products)}"
                )

            await http_client.log(f"BAUHAUS completed: categories={len(leaf_categories)}; products={len(products)}")
            return category_audit, products, self.category_limit is None


async def discover_category_tree(http_client, home_document):
    tree = extract_category_tree(home_document)
    if tree:
        return tree, "HTML"

    best = []
    for attempt in range(1, RSC_ATTEMPTS + 1):
        try:
            rsc_document = await request_home_rsc(http_client, home_document, attempt)
        except HttpRequestError as exc:
            if exc.status == 429:
                await http_client.log("BAUHAUS RSC category source also returned 429; stopping RSC attempts.")
                break
            raise
        candidate = extract_category_tree(rsc_document)
        await http_client.log(f"BAUHAUS RSC attempt: {attempt}/{RSC_ATTEMPTS}; roots={len(candidate)}")
        if len(candidate) > len(best):
            best = candidate
        if len(best) >= 10:
            break
    return best, "RSC"


async def request_home_rsc(http_client, home_document, attempt):
    token = "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(5))
    headers = {
        **HEADERS,
        "accept": "*/*",
        "rsc": "1",
        "next-url": "/",
        "next-router-prefetch": "1",
        "next-router-segment-prefetch": "/!KGNvbW1vbik",
        "referer": f"{BAUHAUS_WEBSITE_URL}/",
    }
    deployment_id = extract_deployment_id(home_document)
    if deployment_id:
        headers["x-deployment-id"] = deployment_id
    return await http_client.get_text(
        f"{BAUHAUS_WEBSITE_URL}/",
        headers=headers,
        params={"_rsc": token},
        endpoint_name=f"RSC attempt {attempt}/{RSC_ATTEMPTS}",
    )


async def fetch_category_products(http_client, category):
    first_html = await http_client.get_text(category.url, endpoint_name="category first page")
    first_hits = extract_hits_from_document(first_html)
    metadata = extract_catalog_metadata(first_html)
    await http_client.log(
        f"BAUHAUS first page: products={len(first_hits)}; hits={metadata['nb_hits']}; pages={metadata['nb_pages']}"
    )
    if not first_hits:
        first_html = await http_client.get_text(category.url, endpoint_name="category first page retry")
        first_hits = extract_hits_from_document(first_html)
        metadata = extract_catalog_metadata(first_html)

    hits_per_page = metadata["hits_per_page"] or len(first_hits) or 40
    nb_hits = metadata["nb_hits"] or len(first_hits)
    reported_pages = metadata["nb_pages"] or 1
    calculated_pages = max(1, math.ceil(nb_hits / hits_per_page))
    expected_pages = min(reported_pages if reported_pages > 0 else calculated_pages, MAX_PAGES_PER_CATEGORY)

    products = products_from_hits(first_hits, category)
    pages_done = 1

    for page in range(2, expected_pages + 1):
        page_url = add_query_parameter(category.url, "page", page)
        hits = []
        for attempt in range(1, EMPTY_PAGE_RETRIES + 2):
            page_html = await http_client.get_text(page_url, endpoint_name=f"category page {page}")
            hits = extract_hits_from_document(page_html)
            if hits or attempt > EMPTY_PAGE_RETRIES:
                break
            delay = random.uniform(3.0 * attempt, 8.0 * attempt)
            await http_client.log(f"BAUHAUS category page {page} unexpectedly empty; retry {attempt}/{EMPTY_PAGE_RETRIES} in {delay:.1f}s")
            await asyncio.sleep(delay)
        products.extend(products_from_hits(hits, category))
        pages_done += 1

    return products, pages_done


def products_from_hits(hits, category):
    return [product for product in (product_from_hit(hit, category) for hit in hits) if product and product.external_id]


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


def extract_category_tree(document):
    best = []
    for version in document_versions(document):
        for value in json_values_after_marker(version, '"categories":'):
            if not isinstance(value, list):
                continue
            roots = [item for item in value if is_catalog_category_node(item)]
            if len(roots) > len(best):
                best = roots
    return best


def is_catalog_category_node(value):
    if not isinstance(value, dict):
        return False
    url_path = clean_text(value.get("url_path")).strip("/")
    children = value.get("children")
    if not url_path or "/" in url_path:
        return False
    if not isinstance(children, list) or not children:
        return False
    return any(
        isinstance(child, dict) and clean_text(child.get("url_path")).startswith(url_path + "/")
        for child in children
    )


def extract_deployment_id(document):
    matches = re.findall(r"dpl_[A-Za-z0-9_-]+", document)
    return matches[0] if matches else ""


def category_urls_from_tree(tree):
    roots_count = 0
    categories = []
    seen_nodes = set()
    seen_leaf_urls = set()
    saved_categories = {}

    def valid_node(node):
        return isinstance(node, dict) and bool(clean_text(node.get("url_path")).strip("/"))

    def walk(node, depth=0, parent_external_id="", parent_path=""):
        url_path = clean_text(node.get("url_path")).strip("/")
        if not url_path:
            return
        url = normalize_url("/" + url_path)
        if not url or url in seen_nodes:
            return
        seen_nodes.add(url)
        name = clean_text(node.get("name")) or url_path.rsplit("/", 1)[-1]
        external_id = stable_external_id(url)
        path = f"{parent_path} > {name}" if parent_path else name
        children = node.get("children")
        valid_children = [child for child in children if valid_node(child)] if isinstance(children, list) else []
        saved_categories[external_id] = StoreCategory(
            external_id=external_id,
            name=name,
            url=url,
            parent_external_id=parent_external_id,
        )
        if valid_children:
            for child in valid_children:
                walk(child, depth + 1, external_id, path)
            return
        if url not in seen_leaf_urls:
            seen_leaf_urls.add(url)
            categories.append(
                StoreCategory(
                    external_id=external_id,
                    name=name,
                    url=url,
                    parent_external_id=parent_external_id,
                )
            )

    for node in tree:
        if not valid_node(node):
            continue
        url_path = clean_text(node.get("url_path")).strip("/")
        if "/" in url_path:
            continue
        if url_path.lower() in TOP_LEVEL_EXCLUDED_SLUGS:
            continue
        roots_count += 1
        walk(node)

    return roots_count, categories, list(saved_categories.values())


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

        category_limit = category_limit_from_env()
        self.allow_incomplete_import = category_limit is not None
        client = BauhausClient(log_callback=live_log, category_limit=category_limit)
        return await client.fetch_products()


def category_limit_from_env():
    value = os.environ.get("BAUHAUS_CATEGORY_LIMIT", "").strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None
