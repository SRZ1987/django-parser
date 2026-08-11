from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import aiohttp
from bs4 import BeautifulSoup

from .public_commerce_parser import (
    COLUMNS,
    MAX_RETRIES,
    RETRYABLE_STATUSES,
    RETRY_BASE_DELAY,
    WORKSHEET_NAME,
    clean_text,
    extract_barcode,
    log,
    normalize_barcode,
    parse_decimal_money,
    retry_after_seconds,
    save_excel,
)


CONCURRENCY = 5
REQUEST_TIMEOUT = 45
PRODUCT_MARKUP_RETRIES = 3


@dataclass(frozen=True)
class SitemapRetailer:
    code: str
    name: str
    base_url: str
    sitemap_url: str


SITEMAP_RETAILERS = {
    store.code: store
    for store in (
        SitemapRetailer(
            code="vipex",
            name="Vipex",
            base_url="https://www.vipex.ee/",
            sitemap_url="https://www.vipex.ee/sitemap_et.xml",
        ),
        SitemapRetailer(
            code="effex",
            name="Effex",
            base_url="https://effex.ee/et/",
            sitemap_url="https://effex.ee/1_et_0_sitemap.xml",
        ),
    )
}


class ProductPageMissing(Exception):
    pass


class ProductMarkupMissing(RuntimeError):
    pass


def redirected_to_store_home(requested_url: str, final_url: str, base_url: str) -> bool:
    requested = urlparse(requested_url)
    final = urlparse(final_url)
    base = urlparse(base_url)
    return bool(
        requested.path.rstrip("/")
        and final.netloc.casefold() == base.netloc.casefold()
        and final.path.rstrip("/") == base.path.rstrip("/")
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap_products(xml_text: str) -> list[tuple[str, str]]:
    root = ElementTree.fromstring(xml_text)
    products: list[tuple[str, str]] = []
    for entry in root:
        if _local_name(entry.tag) != "url":
            continue
        location = ""
        image_url = ""
        for node in entry.iter():
            node_name = _local_name(node.tag)
            if node_name == "loc" and node.text:
                if not location:
                    location = clean_text(node.text)
                elif not image_url:
                    image_url = clean_text(node.text)
        if location and image_url:
            products.append((location, image_url))
    return list(dict.fromkeys(products))


async def request_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
    store: SitemapRetailer,
    label: str,
    log_callback=None,
) -> str:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    if redirected_to_store_home(url, str(response.url), store.base_url):
                        raise ProductPageMissing(url)
                    return await response.text()
                if response.status == 404:
                    raise ProductPageMissing(url)

                body = (await response.text())[:300]
                if response.status not in RETRYABLE_STATUSES or attempt >= MAX_RETRIES:
                    raise RuntimeError(
                        f"{store.name} {label} failed with HTTP {response.status}: {body}"
                    )

                delay = retry_after_seconds(response.headers.get("Retry-After"))
                if delay is None:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
                log(
                    f"{store.name} HTTP {response.status}: {label}, "
                    f"retry={attempt}/{MAX_RETRIES}, delay={delay:.1f}s",
                    log_callback,
                )
                await asyncio.sleep(delay)
        except ProductPageMissing:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    f"{store.name} {label} failed after retries: {exc}"
                ) from exc
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
            log(
                f"{store.name} request error: {label}, retry={attempt}/{MAX_RETRIES}, "
                f"delay={delay:.1f}s, error={exc}",
                log_callback,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"{store.name} {label} exhausted retries.")


def iter_jsonld_products(value: Any):
    if isinstance(value, dict):
        item_type = value.get("@type")
        item_types = item_type if isinstance(item_type, list) else [item_type]
        if any(str(entry).casefold() in {"product", "productgroup"} for entry in item_types):
            yield value
        for graph_item in value.get("@graph") or []:
            yield from iter_jsonld_products(graph_item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_jsonld_products(item)


def extract_jsonld(soup: BeautifulSoup) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    product: dict[str, Any] = {}
    breadcrumbs: list[dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        if not product:
            product = next(iter(iter_jsonld_products(payload)), {})
        values = payload if isinstance(payload, list) else [payload]
        for value in values:
            if isinstance(value, dict) and str(value.get("@type")).casefold() == "breadcrumblist":
                breadcrumbs = [
                    item for item in value.get("itemListElement") or [] if isinstance(item, dict)
                ]
    return product, breadcrumbs


def first_offer(product: dict[str, Any]) -> dict[str, Any]:
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        return next((offer for offer in offers if isinstance(offer, dict)), {})
    return offers if isinstance(offers, dict) else {}


def product_image(product: dict[str, Any], fallback: str) -> str:
    image = product.get("image") or fallback
    if isinstance(image, list):
        image = image[0] if image else fallback
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl") or fallback
    return clean_text(image)


def is_in_stock(offer: dict[str, Any]) -> bool:
    availability = clean_text(offer.get("availability")).casefold().rstrip("/")
    return not availability or availability.endswith("instock")


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def parse_vipex_page(html_text: str, product_url: str, fallback_image: str = "") -> list[list[Any]]:
    soup = BeautifulSoup(html_text, "html.parser")
    product, breadcrumbs = extract_jsonld(soup)
    if not product:
        raise ProductMarkupMissing(f"Vipex product JSON-LD is missing: {product_url}")

    offer = first_offer(product)
    if not is_in_stock(offer):
        return []

    name = clean_text(product.get("name"))
    sku = clean_text(product.get("sku") or offer.get("sku"))
    current_price = parse_decimal_money(offer.get("price"))
    if not name or not sku or current_price == "":
        return []

    old_price_node = soup.select_one(
        ".old-price [data-price-amount], [data-price-type='oldPrice'][data-price-amount]"
    )
    old_price = (
        parse_decimal_money(old_price_node.get("data-price-amount"))
        if old_price_node is not None
        else ""
    )
    if old_price != "" and current_price < old_price:
        price, sale_price = old_price, current_price
    else:
        price, sale_price = current_price, ""

    page_classes = " ".join(soup.body.get("class", [])) if soup.body else ""
    product_id_match = re.search(r"\bcatalog_product_view_id_(\d+)\b", page_classes)
    product_id = product_id_match.group(1) if product_id_match else stable_hash(product_url)

    category_name = ""
    category_url = ""
    if breadcrumbs:
        last_item = breadcrumbs[-1].get("item") or {}
        if isinstance(last_item, dict):
            category_name = clean_text(last_item.get("name"))
            category_url = clean_text(last_item.get("@id"))

    return [[
        name,
        price,
        sale_price,
        "",
        extract_barcode(product),
        f"vipex-{product_id}",
        product_image(product, fallback_image),
        product_url,
        sku,
        category_name,
        f"vipex-category-{stable_hash(category_url or category_name)}" if category_name else "",
        clean_text(product.get("description")),
    ]]


def _price_from_node(node) -> float | str:
    return parse_decimal_money(clean_text(node.get_text(" ")).replace("€", "").replace(" ", ""))


def _effex_product_id(product_url: str) -> str:
    match = re.search(r"/(\d+)(?:-|\.html)", urlparse(product_url).path)
    return match.group(1) if match else stable_hash(product_url)


def _effex_variant_prices(cells) -> tuple[float | str, float | str]:
    net_cell = cells[3]
    gross_cell = cells[4]
    net_current_node = net_cell.select_one("strong")
    gross_current_node = gross_cell.select_one("strong")
    current_net = _price_from_node(net_current_node) if net_current_node else ""
    current_gross = _price_from_node(gross_current_node) if gross_current_node else ""
    if current_gross == "":
        return "", ""

    old_net_node = net_cell.select_one(".base-price")
    old_net = _price_from_node(old_net_node) if old_net_node else ""
    if old_net != "" and current_net not in ("", 0) and old_net > current_net:
        regular_gross = round(old_net * current_gross / current_net, 2)
        if regular_gross > current_gross:
            return regular_gross, current_gross
    return current_gross, ""


def parse_effex_page(html_text: str, product_url: str, fallback_image: str = "") -> list[list[Any]]:
    soup = BeautifulSoup(html_text, "html.parser")
    product, _breadcrumbs = extract_jsonld(soup)
    if not product:
        raise ProductMarkupMissing(f"Effex product JSON-LD is missing: {product_url}")

    offer = first_offer(product)
    if not is_in_stock(offer):
        return []

    product_id = _effex_product_id(product_url)
    category_name = clean_text(product.get("category"))
    category_id = (
        f"effex-category-{stable_hash(category_name.casefold())}" if category_name else ""
    )
    image_url = product_image(product, fallback_image)
    description = clean_text(product.get("description"))

    rows: list[list[Any]] = []
    for row in soup.select(".variations.visible--desktop tr.product-row"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 5:
            continue
        code_match = re.search(r"[A-Za-z0-9][A-Za-z0-9._/-]*", clean_text(cells[0].get_text(" ")))
        code = code_match.group(0) if code_match else ""
        name = clean_text(cells[1].get_text(" "))
        attribute_node = row.select_one("[data-attribute]")
        attribute_id = clean_text(attribute_node.get("data-attribute")) if attribute_node else ""
        price, sale_price = _effex_variant_prices(cells)
        if not name or not attribute_id or price == "":
            continue
        rows.append([
            name,
            price,
            sale_price,
            "",
            normalize_barcode(code),
            f"effex-{product_id}-{attribute_id}",
            image_url,
            product_url,
            code,
            category_name,
            category_id,
            description,
        ])
    if rows:
        return rows

    name = clean_text(product.get("name"))
    current_price = parse_decimal_money(offer.get("price"))
    if not name or current_price == "":
        return []
    standard_price_node = soup.select_one('meta[property="product:price:standard_amount"]')
    standard_price = (
        parse_decimal_money(standard_price_node.get("content"))
        if standard_price_node is not None
        else ""
    )
    if standard_price != "" and standard_price > current_price:
        price, sale_price = standard_price, current_price
    else:
        price, sale_price = current_price, ""
    sku = clean_text(product.get("sku") or offer.get("sku"))
    return [[
        name,
        price,
        sale_price,
        "",
        extract_barcode(product),
        f"effex-{product_id}",
        image_url,
        product_url,
        sku,
        category_name,
        category_id,
        description,
    ]]


PARSERS = {
    "vipex": parse_vipex_page,
    "effex": parse_effex_page,
}


async def fetch_rows(
    session: aiohttp.ClientSession,
    store: SitemapRetailer,
    products: list[tuple[str, str]],
    log_callback=None,
) -> tuple[list[list[Any]], int]:
    limiter = asyncio.Semaphore(CONCURRENCY)
    completed = 0
    missing = 0
    skipped = 0
    progress_lock = asyncio.Lock()
    parser = PARSERS[store.code]

    async def fetch_one(index: int, product: tuple[str, str]):
        nonlocal completed, missing, skipped
        product_url, fallback_image = product
        try:
            for markup_attempt in range(1, PRODUCT_MARKUP_RETRIES + 1):
                async with limiter:
                    html_text = await request_text(
                        session,
                        product_url,
                        store=store,
                        label=f"product {index}/{len(products)}",
                        log_callback=log_callback,
                    )
                try:
                    rows = parser(html_text, product_url, fallback_image)
                    break
                except ProductMarkupMissing as exc:
                    if markup_attempt >= PRODUCT_MARKUP_RETRIES:
                        raise
                    delay = RETRY_BASE_DELAY * (2 ** (markup_attempt - 1))
                    log(
                        f"WARNING: {exc}; retry={markup_attempt}/{PRODUCT_MARKUP_RETRIES}, "
                        f"delay={delay:.1f}s",
                        log_callback,
                    )
                    await asyncio.sleep(delay)
            if not rows:
                skipped += 1
            return rows
        except ProductPageMissing:
            missing += 1
            log(
                f"WARNING: {store.name} product was removed or redirected to the store home: "
                f"{product_url}",
                log_callback,
            )
            return []
        finally:
            async with progress_lock:
                completed += 1
                if completed == len(products) or completed % 100 == 0:
                    log(
                        f"{store.name} download progress: cards={completed}/{len(products)}, "
                        f"missing={missing}, skipped={skipped}",
                        log_callback,
                    )

    row_groups = await asyncio.gather(
        *(fetch_one(index, product) for index, product in enumerate(products, start=1))
    )
    rows = [row for group in row_groups for row in group]
    if not rows:
        raise RuntimeError(f"{store.name} catalog contains no available products.")
    external_ids = [clean_text(row[5]) for row in rows]
    if len(external_ids) != len(set(external_ids)):
        raise RuntimeError(f"{store.name} catalog contains duplicate external IDs.")
    return rows, missing + skipped


async def main(store_code: str, output_path: str | Path, log_callback=None) -> None:
    try:
        store = SITEMAP_RETAILERS[store_code]
    except KeyError as exc:
        raise ValueError(f"Unknown sitemap retailer: {store_code}") from exc

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, connect=15)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "et-EE,et;q=0.9,en;q=0.7",
        "User-Agent": "PriceCompareCatalogBot/1.0",
    }
    async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
        sitemap_xml = await request_text(
            session,
            store.sitemap_url,
            store=store,
            label="product sitemap",
            log_callback=log_callback,
        )
        products = parse_sitemap_products(sitemap_xml)
        if not products:
            raise RuntimeError(f"{store.name} sitemap contains no product cards.")
        log(f"{store.name} sitemap: product_cards={len(products)}", log_callback)
        rows, skipped = await fetch_rows(session, store, products, log_callback)

    destination = Path(output_path)
    await asyncio.to_thread(save_excel, rows, destination)
    log(
        f"{store.name} Excel created: path={destination}, products={len(rows)}, "
        f"skipped={skipped}",
        log_callback,
    )
