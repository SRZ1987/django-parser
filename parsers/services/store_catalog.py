import asyncio
import hashlib
import html
import random
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

import aiohttp
from django.db import transaction
from django.utils import timezone

from catalog.models import Category, PriceHistory, Product, ProductOffer
from catalog.services.normalization import (
    normalize_brand,
    normalize_model,
    normalize_product_name,
    normalize_text,
)

from .base import BaseStoreParser, ParserError, ParserResult


SPACE_RE = re.compile(r"\s+")
PRICE_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
BARCODE_RE = re.compile(r"[\s\u00a0\-]+")


@dataclass
class StoreCategory:
    external_id: str
    name: str
    url: str = ""
    parent_external_id: str = ""


@dataclass
class StoreProduct:
    external_id: str
    name: str
    sku: str = ""
    barcode: str = ""
    brand: str = ""
    model: str = ""
    description: str = ""
    category_external_id: str = ""
    category_name: str = ""
    category_url: str = ""
    price: Decimal | None = None
    sale_price: Decimal | None = None
    currency: str = "EUR"
    product_url: str = ""
    image_url: str = ""
    is_available: bool = True


def clean_text(value):
    if value in (None, ""):
        return ""
    if isinstance(value, list):
        value = " ".join(str(item) for item in value if item is not None)
    text = re.sub(r"<[^>]+>", " ", html.unescape(str(value))).replace("\xa0", " ")
    return SPACE_RE.sub(" ", text).strip()


def parse_decimal(value):
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    text = clean_text(value).replace("€", "").replace("EUR", "").replace(" ", "").replace(",", ".")
    match = PRICE_RE.search(text)
    if not match:
        return None
    try:
        amount = Decimal(match.group(0).replace(",", ".")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount > 0 else None


def clean_barcode(value):
    barcode = clean_text(value)
    if re.fullmatch(r"\d+\.0", barcode):
        barcode = barcode[:-2]
    return BARCODE_RE.sub("", barcode)


def absolute_url(value, base_url):
    url = clean_text(value)
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    return urljoin(base_url, url)


def nested_get(data, *keys, default=None):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def stable_external_id(*parts):
    source = "|".join(clean_text(part) for part in parts if clean_text(part))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:32] if source else ""


def choose_price_pair(regular_price, sale_price):
    price = parse_decimal(regular_price)
    sale = parse_decimal(sale_price)
    if price is not None and sale is not None and sale >= price:
        sale = None
    if price is None and sale is not None:
        price = sale
        sale = None
    return price, sale


class HttpRequestError(ParserError):
    pass


class AsyncStoreClient:
    base_url = ""
    headers = {}
    timeout_seconds = 60
    max_retries = 4
    retry_base_delay = 1.0
    concurrency = 5

    def __init__(self, log_callback=None):
        self.log_callback = log_callback
        self.session = None
        self.semaphore = asyncio.Semaphore(self.concurrency)

    async def __aenter__(self):
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        connector = aiohttp.TCPConnector(limit=self.concurrency, limit_per_host=self.concurrency)
        self.session = aiohttp.ClientSession(headers=self.headers, timeout=timeout, connector=connector)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.session:
            await self.session.close()

    async def log(self, message):
        if self.log_callback:
            result = self.log_callback(message)
            if asyncio.iscoroutine(result):
                await result

    async def request_text(self, method, url, **kwargs):
        return await self._request(method, url, response_type="text", **kwargs)

    async def request_json(self, method, url, **kwargs):
        return await self._request(method, url, response_type="json", **kwargs)

    async def _request(self, method, url, *, response_type, **kwargs):
        if self.session is None:
            raise RuntimeError("HTTP client is not started.")

        async with self.semaphore:
            last_error = None
            for attempt in range(1, self.max_retries + 1):
                try:
                    async with self.session.request(method, url, allow_redirects=True, **kwargs) as response:
                        if response.status == 200:
                            if response_type == "json":
                                return await response.json(content_type=None)
                            return await response.text(errors="replace")
                        if response.status in {408, 425, 429, 500, 502, 503, 504}:
                            retry_after = response.headers.get("Retry-After")
                            try:
                                delay = float(retry_after) if retry_after else 0
                            except (TypeError, ValueError):
                                delay = 0
                            if delay <= 0:
                                delay = min(60, self.retry_base_delay * (2 ** (attempt - 1)) + random.uniform(0.2, 1.0))
                            await self.log(f"HTTP {response.status}: retry {attempt}/{self.max_retries} in {delay:.1f}s for {url}")
                            await asyncio.sleep(delay)
                            continue
                        body = await response.text(errors="replace")
                        raise HttpRequestError(f"HTTP {response.status}: {body[:300]}")
                except (aiohttp.ClientError, asyncio.TimeoutError, HttpRequestError) as exc:
                    last_error = exc
                    if attempt >= self.max_retries:
                        break
                    delay = min(60, self.retry_base_delay * (2 ** (attempt - 1)) + random.uniform(0.2, 1.0))
                    await self.log(f"Request error: {exc}; retry {attempt}/{self.max_retries} in {delay:.1f}s for {url}")
                    await asyncio.sleep(delay)

            raise HttpRequestError(f"Request failed after {self.max_retries} attempts: {url}. Last error: {last_error}")


class StoreCatalogParser(BaseStoreParser):
    shop_name = ""
    website_url = ""
    DEACTIVATE_BATCH_SIZE = 1000
    MIN_ACTIVE_OFFERS_FOR_ANOMALY_CHECK = 100
    MIN_REMOTE_TO_ACTIVE_RATIO = 0.2

    def run(self):
        seen_at = timezone.now()
        shop = self.parser_config.shop
        result = ParserResult()

        self.log(f"{self.code.upper()} parser started.")
        raw_categories, raw_products, complete = self._run_async(self._fetch_remote_data())

        if not complete:
            raise ParserError(f"{self.code.upper()} catalog was not fully loaded; refusing to deactivate existing offers.")
        if not raw_products:
            raise ParserError(f"{self.code.upper()} returned an empty product list; refusing to deactivate existing offers.")

        categories = self._save_categories(shop, raw_categories)
        unique_products = self._remove_duplicates(raw_products)
        remote_external_ids = {product.external_id for product in unique_products if product.external_id}
        result.products_found = len(unique_products)
        self._validate_remote_catalog_size(shop, remote_external_ids)

        for index, product in enumerate(unique_products, start=1):
            try:
                category = categories.get(product.category_external_id)
                if category is None and product.category_external_id and product.category_name:
                    category = self._save_inline_category(shop, product)
                    categories[product.category_external_id] = category
                created, price_changed = self._save_product_offer(shop, category, product, seen_at)
                result.products_created += int(created)
                result.products_updated += int(not created)
                result.prices_changed += int(price_changed)
                if index % 250 == 0:
                    self.log(f"{self.code.upper()} progress: {index}/{result.products_found} products saved.")
            except Exception as exc:
                result.errors_count += 1
                self.log(f"{self.code.upper()} product error: {exc}")

        self._deactivate_missing_offers(shop, remote_external_ids)
        self.log(
            f"{self.code.upper()} parser finished: found={result.products_found}, "
            f"created={result.products_created}, updated={result.products_updated}, "
            f"prices_changed={result.prices_changed}, errors={result.errors_count}."
        )
        return result

    async def _fetch_remote_data(self):
        raise NotImplementedError

    def _run_async(self, coroutine):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        raise ParserError(f"{self.code.upper()} parser cannot run inside an already running event loop.")

    def _save_categories(self, shop, raw_categories):
        saved = {}
        pending = []
        for raw in raw_categories:
            if not raw.external_id:
                continue
            category, _ = Category.objects.update_or_create(
                shop=shop,
                external_id=raw.external_id,
                defaults={
                    "name": raw.name or self.shop_name or shop.name,
                    "normalized_name": normalize_text(raw.name or self.shop_name or shop.name),
                    "url": raw.url,
                    "parent": None,
                },
            )
            saved[raw.external_id] = category
            if raw.parent_external_id:
                pending.append((category, raw.parent_external_id))

        for category, parent_external_id in pending:
            parent = saved.get(parent_external_id)
            if parent and category.parent_id != parent.pk:
                category.parent = parent
                category.save(update_fields=["parent", "updated_at"])

        self.log(f"{self.code.upper()} category sync finished: {len(saved)} categories.")
        return saved

    def _save_inline_category(self, shop, product):
        category, _ = Category.objects.update_or_create(
            shop=shop,
            external_id=product.category_external_id,
            defaults={
                "name": product.category_name,
                "normalized_name": normalize_text(product.category_name),
                "url": product.category_url,
                "parent": None,
            },
        )
        return category

    @transaction.atomic
    def _save_product_offer(self, shop, category, parsed, seen_at):
        offer = ProductOffer.objects.select_related("product").filter(shop=shop, external_id=parsed.external_id).first()
        created = offer is None
        if created:
            product = Product.objects.create(
                name=parsed.name,
                brand=parsed.brand,
                model=parsed.model,
                barcode=parsed.barcode,
            )
            offer = ProductOffer(shop=shop, product=product, external_id=parsed.external_id)
        else:
            product = offer.product
            product.name = parsed.name
            product.brand = parsed.brand
            product.model = parsed.model
            product.barcode = parsed.barcode
            product.normalized_name = normalize_product_name(parsed.name)
            product.normalized_brand = normalize_brand(parsed.brand)
            product.normalized_model = normalize_model(parsed.model)
            product.save(
                update_fields=[
                    "name",
                    "normalized_name",
                    "brand",
                    "normalized_brand",
                    "model",
                    "normalized_model",
                    "barcode",
                    "updated_at",
                ]
            )

        previous_price = offer.price
        previous_sale_price = offer.sale_price
        offer.category = category
        offer.sku = parsed.sku
        offer.barcode = parsed.barcode
        offer.original_name = parsed.name
        offer.description = parsed.description
        offer.price = parsed.price
        offer.sale_price = parsed.sale_price
        offer.currency = parsed.currency or "EUR"
        offer.product_url = parsed.product_url
        offer.image_url = parsed.image_url
        offer.is_available = parsed.is_available
        offer.is_active = True
        offer.last_seen_at = seen_at
        offer.save()

        price_known = parsed.price is not None or parsed.sale_price is not None
        price_changed = created and price_known or previous_price != parsed.price or previous_sale_price != parsed.sale_price
        if price_changed:
            PriceHistory.objects.create(offer=offer, price=parsed.price, sale_price=parsed.sale_price)
        return created, price_changed

    def _remove_duplicates(self, products):
        unique = {}
        for product in products:
            if product.external_id and product.external_id not in unique:
                unique[product.external_id] = product
        return list(unique.values())

    def _deactivate_missing_offers(self, shop, remote_external_ids):
        missing_offer_ids = [
            offer_id
            for offer_id, external_id in ProductOffer.objects.filter(shop=shop).values_list("id", "external_id")
            if external_id not in remote_external_ids
        ]
        updated = 0
        for start in range(0, len(missing_offer_ids), self.DEACTIVATE_BATCH_SIZE):
            batch = missing_offer_ids[start : start + self.DEACTIVATE_BATCH_SIZE]
            updated += ProductOffer.objects.filter(id__in=batch).update(is_active=False, is_available=False)
        self.log(f"{self.code.upper()} inactive offers marked: {updated}.")

    def _validate_remote_catalog_size(self, shop, remote_external_ids):
        active_count = ProductOffer.objects.filter(shop=shop, is_active=True).count()
        if active_count < self.MIN_ACTIVE_OFFERS_FOR_ANOMALY_CHECK:
            return
        if len(remote_external_ids) >= active_count * self.MIN_REMOTE_TO_ACTIVE_RATIO:
            return
        message = (
            f"{self.code.upper()} returned an anomalously small product list: "
            f"{len(remote_external_ids)} remote products for {active_count} active offers. "
            "Refusing to deactivate existing offers."
        )
        self.log(message)
        raise ParserError(message)
