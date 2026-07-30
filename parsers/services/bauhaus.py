import asyncio
import html
import json
import math
import os
import random
import re
import time
from html.parser import HTMLParser
from typing import Any
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
EAN_CONCURRENCY = 50
EAN_FALLBACK_CONCURRENCY = 10
EAN_RECOVERY_STEP = 10
EAN_RECOVERY_INTERVAL = 10.0
EAN_MAX_CLIENTS = EAN_CONCURRENCY
EAN_MAX_RETRIES = 4
EAN_REQUEST_TIMEOUT = 45
EAN_PROGRESS_INTERVAL = 15.0
EAN_DEGRADED_HTTP_ERROR_RATIO = 0.2

BARCODE_KEYS = (
    "gtin",
    "gtin8",
    "gtin12",
    "gtin13",
    "gtin14",
    "ean",
    "ean8",
    "ean13",
    "ean14",
    "barcode",
)
NEXT_DATA_BARCODE_PATTERN = re.compile(
    r'(?i)(?:\\?")(?:gtin(?:8|12|13|14)?|ean(?:8|13|14)?|barcode)(?:\\?")'
    r'\s*:\s*(?:\\?")(\d{8}|\d{12,14})(?:\\?")'
)

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
    def __init__(self, log_callback=None, category_limit=None, existing_barcodes=None):
        self.log_callback = log_callback
        self.category_limit = category_limit
        self.existing_barcodes = existing_barcodes or {}

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
            apply_existing_barcodes(products, self.existing_barcodes)
            await enrich_all_products_with_ean(products, log_callback=http_client.log)
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


class AdjustableLimiter:
    def __init__(self, limit):
        self._limit = max(1, int(limit))
        self._active = 0
        self._condition = asyncio.Condition()

    @property
    def limit(self):
        return self._limit

    @property
    def active(self):
        return self._active

    async def set_limit(self, value):
        async with self._condition:
            self._limit = max(1, int(value))
            self._condition.notify_all()

    async def __aenter__(self):
        async with self._condition:
            await self._condition.wait_for(lambda: self._active < self._limit)
            self._active += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        async with self._condition:
            self._active = max(0, self._active - 1)
            self._condition.notify_all()


class AdaptiveLoadController:
    def __init__(self, ean_limiter, log_callback=None):
        self.ean_limiter = ean_limiter
        self.log_callback = log_callback
        self.ean_allowed = asyncio.Event()
        self.ean_allowed.set()
        self.draining = False
        self.restrictions = 0
        self.adaptations = 0
        self.last_reason = ""
        self._adaptation_lock = asyncio.Lock()
        self._drain_task = None

    async def log(self, message):
        if self.log_callback:
            result = self.log_callback(message)
            if asyncio.iscoroutine(result):
                await result

    async def wait_ean_allowed(self):
        await self.ean_allowed.wait()

    async def report_restriction(self, reason):
        self.restrictions += 1
        self.last_reason = reason
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._adapt_after_restriction(reason))

    async def _adapt_after_restriction(self, reason):
        async with self._adaptation_lock:
            if self.draining:
                return
            current = self.ean_limiter.limit
            target = max(EAN_FALLBACK_CONCURRENCY, current - EAN_RECOVERY_STEP)
            self.draining = True
            self.adaptations += 1
            self.ean_allowed.clear()
            await self.ean_limiter.set_limit(target)
            await self.log(
                f"BAUHAUS EAN adaptive throttle: {reason}; concurrency={current}->{target}; "
                f"pause={EAN_RECOVERY_INTERVAL:.0f}s"
            )
            await asyncio.sleep(EAN_RECOVERY_INTERVAL)
            self.draining = False
            self.ean_allowed.set()

    async def close(self):
        if self._drain_task is not None:
            self._drain_task.cancel()
            await asyncio.gather(self._drain_task, return_exceptions=True)


class JsonLdScriptParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self._inside_jsonld = False
        self._buffer = []
        self.blocks = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "script":
            return
        attributes = {str(key).lower(): value or "" for key, value in attrs}
        script_type = attributes.get("type", "").lower().split(";", 1)[0].strip()
        if script_type == "application/ld+json":
            self._inside_jsonld = True
            self._buffer = []

    def handle_data(self, data):
        if self._inside_jsonld:
            self._buffer.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._inside_jsonld:
            block = "".join(self._buffer).strip()
            if block:
                self.blocks.append(block)
            self._inside_jsonld = False
            self._buffer = []


def walk_json(value: Any):
    stack = [value]
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def json_type_contains_product(value: Any):
    if isinstance(value, str):
        return value.lower() == "product"
    if isinstance(value, list):
        return any(isinstance(item, str) and item.lower() == "product" for item in value)
    return False


def normalize_barcode_candidate(value: Any):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not value.is_integer():
            return ""
        text = str(int(value))
    else:
        text = clean_text(value)
    digits = re.sub(r"\D", "", text)
    if len(digits) not in {8, 12, 13, 14}:
        return ""
    return digits if is_valid_gtin(digits) else ""


def is_valid_gtin(code):
    if not code.isdigit() or len(code) not in {8, 12, 13, 14}:
        return False
    body = code[:-1]
    expected = int(code[-1])
    total = 0
    for index, char in enumerate(reversed(body)):
        total += int(char) * (3 if index % 2 == 0 else 1)
    calculated = (10 - total % 10) % 10
    return calculated == expected


def barcode_from_mapping(mapping):
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for key in BARCODE_KEYS:
        candidate = normalize_barcode_candidate(lowered.get(key))
        if candidate:
            return candidate
    return ""


def extract_ean_from_jsonld(page_html, expected_sku=""):
    parser = JsonLdScriptParser()
    try:
        parser.feed(page_html)
        parser.close()
    except Exception:
        return ""

    expected_sku = clean_text(expected_sku)
    fallback = ""
    for raw_block in parser.blocks:
        raw_block = html.unescape(raw_block).strip()
        try:
            payload = json.loads(raw_block)
        except json.JSONDecodeError:
            continue
        for node in walk_json(payload):
            if not isinstance(node, dict):
                continue
            if not json_type_contains_product(node.get("@type")):
                continue
            ean = barcode_from_mapping(node)
            if not ean:
                continue
            node_sku = clean_text(node.get("sku"))
            if expected_sku and node_sku and node_sku == expected_sku:
                return ean
            if not fallback:
                fallback = ean
    return fallback


def extract_ean_from_next_data(page_html):
    match = NEXT_DATA_BARCODE_PATTERN.search(page_html)
    if not match:
        return ""
    return normalize_barcode_candidate(match.group(1))


def product_page_has_data(page_html, expected_sku=""):
    text = page_html or ""
    lowered = text.lower()
    if len(text) < 1000 and not any(marker in lowered for marker in ("application/ld+json", "__next", "self.__next_f")):
        return False
    if any(marker in lowered for marker in ("vercel security checkpoint", "security checkpoint", "access denied", "captcha")):
        return False
    expected_sku = clean_text(expected_sku)
    if expected_sku and expected_sku in text:
        return True
    if "application/ld+json" in lowered and "product" in lowered:
        return True
    if ("__next_data__" in lowered or "self.__next_f" in lowered or "__next" in lowered) and any(
        marker in lowered for marker in ("product", "sku", "gtin", "ean", "barcode")
    ):
        return True
    return False


def page_has_invalid_gtin_candidate(page_html):
    for version in document_versions(page_html):
        if re.search(
            r'(?i)(?:\\?")(?:gtin(?:8|12|13|14)?|ean(?:8|13|14)?|barcode)(?:\\?")\s*:\s*(?:\\?")?\d+(?:\\?")?',
            version,
        ):
            return True
    return False


async def fetch_product_ean(session, limiter, controller, sku, product_url):
    async with limiter:
        for attempt in range(1, EAN_MAX_RETRIES + 1):
            try:
                await controller.wait_ean_allowed()
                await asyncio.sleep(random.uniform(0.005, 0.025))
                response = await session.get(
                    product_url,
                    headers={
                        **HEADERS,
                        "accept": "text/html,application/xhtml+xml",
                        "referer": f"{BAUHAUS_WEBSITE_URL}/",
                    },
                    timeout=EAN_REQUEST_TIMEOUT,
                    allow_redirects=True,
                )
                resolved_url = str(getattr(response, "url", product_url))
                status = response.status_code
                if status == 200:
                    page_html = response.text
                    if not product_page_has_data(page_html, expected_sku=sku):
                        if attempt < EAN_MAX_RETRIES:
                            await asyncio.sleep(min(30.0, 1.8 ** attempt + random.uniform(0.5, 1.8)))
                            continue
                        return sku, "", "invalid_html", resolved_url

                    ean = extract_ean_from_jsonld(page_html, expected_sku=sku)
                    if ean:
                        return sku, ean, "jsonld_gtin", resolved_url
                    ean = extract_ean_from_next_data(page_html)
                    if ean:
                        return sku, ean, "next_data_gtin", resolved_url
                    if page_has_invalid_gtin_candidate(page_html):
                        return sku, "", "invalid_gtin", resolved_url
                    return sku, "", "real_not_found", resolved_url

                if status in {403, 408, 429, 500, 502, 503, 504}:
                    if status in {403, 408, 429}:
                        await controller.report_restriction(f"HTTP {status} during EAN request")
                    if attempt == EAN_MAX_RETRIES:
                        return sku, "", "http_errors", resolved_url
                    retry_after = response.headers.get("retry-after") or response.headers.get("Retry-After")
                    wait = retry_delay(attempt, retry_after)
                    await asyncio.sleep(min(wait, 30.0))
                    continue

                return sku, "", "http_errors", resolved_url
            except Exception as error:
                if attempt == EAN_MAX_RETRIES:
                    return sku, "", "request_errors", product_url
                await asyncio.sleep(min(30.0, 1.8 ** attempt + random.uniform(0.5, 1.8)))
    return sku, "", "request_errors", product_url


async def enrich_all_products_with_ean(products, log_callback=None):
    targets = []
    target_by_sku = {}
    for product in products:
        product.barcode = normalize_barcode_candidate(product.barcode)
        if product.barcode:
            continue
        sku = clean_text(product.sku)
        product_url = clean_text(product.product_url)
        if not sku or not product_url or sku in target_by_sku:
            continue
        target_by_sku[sku] = product
        targets.append((sku, product_url))

    async def log(message):
        if log_callback:
            result = log_callback(message)
            if asyncio.iscoroutine(result):
                await result

    if not targets:
        await log("BAUHAUS EAN enrichment skipped: no products need barcode lookup.")
        return ean_stats()

    if CurlAsyncSession is None:
        raise RuntimeError("curl_cffi is required for BAUHAUS EAN enrichment.")

    await log(f"BAUHAUS EAN enrichment started: products={len(products)}; targets={len(targets)}")
    queue = asyncio.Queue()
    for item in targets:
        queue.put_nowait(item)

    limiter = AdjustableLimiter(EAN_CONCURRENCY)
    stats = ean_stats(scheduled=len(targets))
    state_lock = asyncio.Lock()
    started_at = time.monotonic()
    last_progress = started_at

    async with CurlAsyncSession(headers=HEADERS, impersonate="chrome", max_clients=EAN_MAX_CLIENTS) as session:
        controller = AdaptiveLoadController(limiter, log_callback=log)

        async def worker():
            nonlocal last_progress
            while True:
                item = await queue.get()
                try:
                    if item is None:
                        return
                    sku, product_url = item
                    _, ean, source, _ = await fetch_product_ean(session, limiter, controller, sku, product_url)
                    async with state_lock:
                        stats["checked"] += 1
                        if ean:
                            target_by_sku[sku].barcode = ean
                            stats["found"] += 1
                        else:
                            stats[source if source in stats else "request_errors"] += 1
                        now = time.monotonic()
                        if now - last_progress >= EAN_PROGRESS_INTERVAL or stats["checked"] == stats["scheduled"]:
                            last_progress = now
                            await log(
                                "BAUHAUS EAN progress: "
                                f"checked={stats['checked']}/{stats['scheduled']}; "
                                f"found={stats['found']}; real_not_found={stats['real_not_found']}; "
                                f"invalid_html={stats['invalid_html']}; http_errors={stats['http_errors']}; "
                                f"request_errors={stats['request_errors']}; invalid_gtin={stats['invalid_gtin']}; "
                                f"queue={queue.qsize()}; concurrency={limiter.limit}; elapsed={now - started_at:.1f}s"
                            )
                except Exception as error:
                    async with state_lock:
                        stats["checked"] += 1
                        stats["request_errors"] += 1
                    await log(f"BAUHAUS EAN worker error: {type(error).__name__}: {error}")
                finally:
                    queue.task_done()

        worker_count = min(EAN_CONCURRENCY, len(targets))
        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        await queue.join()
        for _ in workers:
            queue.put_nowait(None)
        await queue.join()
        await asyncio.gather(*workers, return_exceptions=True)
        await controller.close()

    if stats["scheduled"] and stats["http_errors"] / stats["scheduled"] > EAN_DEGRADED_HTTP_ERROR_RATIO:
        await log(
            "BAUHAUS EAN enrichment degraded: "
            f"http_errors={stats['http_errors']}; scheduled={stats['scheduled']}"
        )
    await log(
        "BAUHAUS EAN enrichment finished: "
        f"scheduled={stats['scheduled']}; checked={stats['checked']}; found={stats['found']}; "
        f"real_not_found={stats['real_not_found']}; invalid_html={stats['invalid_html']}; "
        f"http_errors={stats['http_errors']}; request_errors={stats['request_errors']}; "
        f"invalid_gtin={stats['invalid_gtin']}; restrictions={controller.restrictions}; "
        f"adaptations={controller.adaptations}"
    )
    return stats


def ean_stats(scheduled=0):
    return {
        "scheduled": scheduled,
        "checked": 0,
        "found": 0,
        "real_not_found": 0,
        "invalid_html": 0,
        "http_errors": 0,
        "request_errors": 0,
        "invalid_gtin": 0,
    }


def apply_existing_barcodes(products, existing_barcodes):
    for product in products:
        current = normalize_barcode_candidate(product.barcode)
        if current:
            product.barcode = current
            continue
        existing = normalize_barcode_candidate(existing_barcodes.get(product.external_id) or existing_barcodes.get(product.sku))
        if existing:
            product.barcode = existing


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
        existing_barcodes = await asyncio.to_thread(self._existing_barcodes)
        client = BauhausClient(
            log_callback=live_log,
            category_limit=category_limit,
            existing_barcodes=existing_barcodes,
        )
        return await client.fetch_products()

    def _existing_barcodes(self):
        return {
            external_id: barcode
            for external_id, barcode in self.parser_config.shop.offers.exclude(barcode="").values_list("external_id", "barcode")
        }


def category_limit_from_env():
    value = os.environ.get("BAUHAUS_CATEGORY_LIMIT", "").strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None
