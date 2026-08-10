from __future__ import annotations

import asyncio
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
    log,
    normalize_barcode,
    parse_decimal_money,
    retry_after_seconds,
    save_excel,
)
from .sitemap_retailers_parser import (
    extract_jsonld,
    first_offer,
    is_in_stock,
    product_image,
    stable_hash,
)


CONCURRENCY = 5
REQUEST_TIMEOUT = 60
MIN_EXPORTED_PRODUCTS = {"torujyri": 1000, "arcade": 500}


@dataclass(frozen=True)
class CatalogSitemapRetailer:
    code: str
    name: str
    base_url: str
    sitemap_url: str


CATALOG_SITEMAP_RETAILERS = {
    store.code: store
    for store in (
        CatalogSitemapRetailer(
            code="torujyri",
            name="Toru-Jüri",
            base_url="https://www.torujyri.ee/",
            sitemap_url="https://www.torujyri.ee/wp-sitemap.xml",
        ),
        CatalogSitemapRetailer(
            code="arcade",
            name="Arcade",
            base_url="https://www.arcade.ee/",
            sitemap_url="https://www.arcade.ee/sitemap.xml",
        ),
    )
}


class ProductPageMissing(Exception):
    pass


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap_locations(xml_text: str) -> list[str]:
    root = ElementTree.fromstring(xml_text)
    locations = []
    for node in root.iter():
        if _local_name(node.tag) == "loc" and node.text:
            location = clean_text(node.text).replace("&amp;", "&")
            if location:
                locations.append(location)
    return list(dict.fromkeys(locations))


async def request_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
    store: CatalogSitemapRetailer,
    label: str,
    log_callback=None,
) -> str:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(url) as response:
                if response.status == 200:
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


def _category_parts(value: Any) -> tuple[str, str]:
    parts = [clean_text(part) for part in clean_text(value).replace("&gt;", ">").split(">")]
    parts = [part for part in parts if part]
    category_name = parts[-1] if parts else ""
    return category_name, "/".join(parts).casefold()


def _price_from_text(value: Any) -> float | str:
    text = clean_text(value)
    match = re.search(r"\d[\d\s]*(?:[,.]\d+)?", text)
    if not match:
        return ""
    return parse_decimal_money(match.group(0).replace(" ", "").replace(",", "."))


def parse_torujyri_page(html_text: str, product_url: str) -> list[Any] | None:
    soup = BeautifulSoup(html_text, "html.parser")
    product, _breadcrumbs = extract_jsonld(soup)
    if not product:
        raise RuntimeError(f"Toru-Juri product JSON-LD is missing: {product_url}")
    offer = first_offer(product)
    if not is_in_stock(offer):
        return None

    heading = soup.select_one("h1.product_title, h1")
    name = clean_text(heading.get_text(" ")) if heading else clean_text(product.get("name"))
    sku = clean_text(product.get("sku") or offer.get("sku"))
    price_container = soup.select_one(".summary .price")
    old_price_node = price_container.select_one("del .woocommerce-Price-amount") if price_container else None
    sale_price_node = price_container.select_one("ins .woocommerce-Price-amount") if price_container else None
    current_price_node = (
        price_container.select_one(".woocommerce-Price-amount") if price_container else None
    )
    if old_price_node is not None and sale_price_node is not None:
        price = _price_from_text(old_price_node.get_text(" "))
        sale_price = _price_from_text(sale_price_node.get_text(" "))
    else:
        price = (
            _price_from_text(current_price_node.get_text(" "))
            if current_price_node is not None
            else parse_decimal_money(offer.get("price"))
        )
        sale_price = ""
    if not name or not sku or price == "":
        return None

    category_name, category_path = _category_parts(product.get("category"))
    return [
        name,
        price,
        sale_price,
        "",
        normalize_barcode(
            product.get("gtin13")
            or product.get("gtin14")
            or product.get("gtin12")
            or product.get("gtin8")
        ),
        f"torujyri-{stable_hash(product_url)}",
        product_image(product, ""),
        product_url,
        sku,
        category_name,
        f"torujyri-category-{stable_hash(category_path)}" if category_path else "",
        clean_text(product.get("description")),
    ]


def parse_arcade_page(html_text: str, product_url: str) -> list[Any] | None:
    soup = BeautifulSoup(html_text, "html.parser")
    product_form = soup.select_one("form#product_addtocart_form")
    product_id_node = soup.select_one('input[name="product"]')
    if product_form is None or product_id_node is None:
        return None
    availability = soup.select_one(".availability")
    if availability is not None and "out-of-stock" in (availability.get("class") or []):
        return None

    product_id = clean_text(product_id_node.get("value"))
    name_node = soup.select_one("h1.product-name, h1")
    name = clean_text(name_node.get_text(" ")) if name_node else ""
    old_price_node = soup.select_one(".old-price .price")
    sale_price_node = soup.select_one(".special-price .price")
    regular_price_node = soup.select_one(".regular-price .price, .price-box .price")
    if old_price_node is not None and sale_price_node is not None:
        price = _price_from_text(old_price_node.get_text(" "))
        sale_price = _price_from_text(sale_price_node.get_text(" "))
    else:
        price = _price_from_text(regular_price_node.get_text(" ")) if regular_price_node else ""
        sale_price = ""
    if not product_id or not name or price == "":
        return None

    image = soup.select_one("img#image")
    category_links = soup.select(".breadcrumbs li:not(.home) a")
    category_name = clean_text(category_links[-1].get_text(" ")) if category_links else ""
    category_url = clean_text(category_links[-1].get("href")) if category_links else ""
    description = soup.select_one(".short-description .std, .box-description .std")
    return [
        name,
        price,
        sale_price,
        "",
        "",
        f"arcade-{product_id}",
        clean_text(image.get("src")) if image else "",
        product_url,
        product_id,
        category_name,
        f"arcade-category-{stable_hash(category_url)}" if category_url else "",
        clean_text(description.get_text(" ")) if description else "",
    ]


async def discover_product_urls(
    session: aiohttp.ClientSession,
    store: CatalogSitemapRetailer,
    log_callback=None,
) -> list[str]:
    root_xml = await request_text(
        session,
        store.sitemap_url,
        store=store,
        label="sitemap index",
        log_callback=log_callback,
    )
    root_locations = parse_sitemap_locations(root_xml)
    if store.code == "arcade":
        return [url for url in root_locations if urlparse(url).path.endswith(".html")]

    product_sitemaps = [
        url
        for url in root_locations
        if re.search(r"/product-sitemap\d+\.xml$", urlparse(url).path)
    ]
    if not product_sitemaps:
        raise RuntimeError("Toru-Jüri product sitemap index is empty.")
    sitemap_payloads = await asyncio.gather(
        *(
            request_text(
                session,
                sitemap_url,
                store=store,
                label=f"product sitemap {index}/{len(product_sitemaps)}",
                log_callback=log_callback,
            )
            for index, sitemap_url in enumerate(product_sitemaps, start=1)
        )
    )
    product_urls = []
    for payload in sitemap_payloads:
        product_urls.extend(
            url for url in parse_sitemap_locations(payload) if "/toode/" in urlparse(url).path
        )
    return list(dict.fromkeys(product_urls))


async def fetch_rows(
    session: aiohttp.ClientSession,
    store: CatalogSitemapRetailer,
    product_urls: list[str],
    log_callback=None,
) -> tuple[list[list[Any]], int]:
    limiter = asyncio.Semaphore(CONCURRENCY)
    completed = 0
    skipped = 0
    missing = 0
    progress_lock = asyncio.Lock()
    parser = parse_torujyri_page if store.code == "torujyri" else parse_arcade_page

    async def fetch_one(index: int, product_url: str):
        nonlocal completed, skipped, missing
        try:
            async with limiter:
                html_text = await request_text(
                    session,
                    product_url,
                    store=store,
                    label=f"product {index}/{len(product_urls)}",
                    log_callback=log_callback,
                )
            row = parser(html_text, product_url)
            if row is None:
                skipped += 1
            return row
        except ProductPageMissing:
            missing += 1
            return None
        finally:
            async with progress_lock:
                completed += 1
                if completed == len(product_urls) or completed % 100 == 0:
                    log(
                        f"{store.name} download progress: cards={completed}/{len(product_urls)}, "
                        f"missing={missing}, skipped={skipped}",
                        log_callback,
                    )

    results = await asyncio.gather(
        *(fetch_one(index, url) for index, url in enumerate(product_urls, start=1))
    )
    rows_by_id: dict[str, list[Any]] = {}
    for row in results:
        if row is None:
            continue
        external_id = row[5]
        existing = rows_by_id.get(external_id)
        if existing is not None and existing[7] != row[7]:
            raise RuntimeError(
                f"{store.name} duplicate external_id {external_id!r} has different URLs."
            )
        rows_by_id[external_id] = row
    rows = sorted(rows_by_id.values(), key=lambda row: (str(row[0]).casefold(), str(row[5])))
    minimum = MIN_EXPORTED_PRODUCTS[store.code]
    if len(rows) < minimum:
        raise RuntimeError(
            f"{store.name} catalog is unexpectedly small: products={len(rows)}, "
            f"minimum={minimum}."
        )
    return rows, missing + skipped


async def main(store_code: str, output_path: str | Path, log_callback=None) -> None:
    try:
        store = CATALOG_SITEMAP_RETAILERS[store_code]
    except KeyError as exc:
        raise ValueError(f"Unknown catalog sitemap retailer: {store_code}") from exc

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, connect=20)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "et-EE,et;q=0.9,en;q=0.7",
        "User-Agent": "PriceCompareCatalogBot/1.0",
    }
    async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
        product_urls = await discover_product_urls(session, store, log_callback)
        if not product_urls:
            raise RuntimeError(f"{store.name} sitemap contains no product cards.")
        log(f"{store.name} sitemap: product_cards={len(product_urls)}", log_callback)
        rows, skipped = await fetch_rows(session, store, product_urls, log_callback)

    destination = Path(output_path)
    await asyncio.to_thread(save_excel, rows, destination)
    log(
        f"{store.name} Excel created: path={destination}, products={len(rows)}, skipped={skipped}",
        log_callback,
    )
