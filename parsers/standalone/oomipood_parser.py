from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
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


BASE_URL = "https://www.oomipood.ee/"
SITEMAP_URL = urljoin(BASE_URL, "sitemap.xml")
CONCURRENCY = 6
REQUEST_TIMEOUT = 45


class ProductPageMissing(Exception):
    pass


async def request_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
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
                        f"Oomipood {label} failed with HTTP {response.status}: {body}"
                    )

                delay = retry_after_seconds(response.headers.get("Retry-After"))
                if delay is None:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
                log(
                    f"Oomipood HTTP {response.status}: {label}, "
                    f"retry={attempt}/{MAX_RETRIES}, delay={delay:.1f}s",
                    log_callback,
                )
                await asyncio.sleep(delay)
        except ProductPageMissing:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt >= MAX_RETRIES:
                raise RuntimeError(f"Oomipood {label} failed after retries: {exc}") from exc
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
            log(
                f"Oomipood request error: {label}, retry={attempt}/{MAX_RETRIES}, "
                f"delay={delay:.1f}s, error={exc}",
                log_callback,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"Oomipood {label} exhausted retries.")


def parse_sitemap_locations(xml_text: str) -> list[str]:
    root = ElementTree.fromstring(xml_text)
    return [clean_text(node.text) for node in root.iter() if node.tag.endswith("loc") and node.text]


async def fetch_product_urls(session: aiohttp.ClientSession, log_callback=None) -> list[str]:
    index_xml = await request_text(
        session,
        SITEMAP_URL,
        label="sitemap index",
        log_callback=log_callback,
    )
    locations = parse_sitemap_locations(index_xml)
    product_sitemaps = [url for url in locations if "sitemap_product" in url]
    if not product_sitemaps:
        product_urls = [url for url in locations if "/product/" in url]
    else:
        sitemap_documents = await asyncio.gather(
            *(
                request_text(
                    session,
                    sitemap_url,
                    label=f"product sitemap {index + 1}/{len(product_sitemaps)}",
                    log_callback=log_callback,
                )
                for index, sitemap_url in enumerate(product_sitemaps)
            )
        )
        product_urls = [
            url
            for document in sitemap_documents
            for url in parse_sitemap_locations(document)
            if "/product/" in url
        ]

    unique_urls = list(dict.fromkeys(product_urls))
    if not unique_urls:
        raise RuntimeError("Oomipood sitemap contains no product URLs.")
    log(
        f"Oomipood sitemap: product_sitemaps={len(product_sitemaps)}, "
        f"product_urls={len(unique_urls)}",
        log_callback,
    )
    return unique_urls


def iter_jsonld_products(value: Any):
    if isinstance(value, dict):
        item_type = value.get("@type")
        types = item_type if isinstance(item_type, list) else [item_type]
        if any(str(entry).casefold() == "product" for entry in types):
            yield value
        for graph_item in value.get("@graph") or []:
            yield from iter_jsonld_products(graph_item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_jsonld_products(item)


def parse_price_text(value: str) -> float | str:
    match = re.search(r"\d[\d\s.,]*", clean_text(value))
    if not match:
        return ""
    number = match.group(0).replace(" ", "")
    if number.count(",") == 1 and number.count(".") == 0:
        number = number.replace(",", ".")
    elif number.count(".") > 1:
        head, tail = number.rsplit(".", 1)
        number = head.replace(".", "") + "." + tail
    return parse_decimal_money(number)


def first_offer(product: dict[str, Any]) -> dict[str, Any]:
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        return next((offer for offer in offers if isinstance(offer, dict)), {})
    return offers if isinstance(offers, dict) else {}


def parse_product_page(html_text: str, product_url: str) -> list[Any] | None:
    soup = BeautifulSoup(html_text, "html.parser")
    product = None
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        product = next(iter(iter_jsonld_products(payload)), None)
        if product is not None:
            break
    if product is None:
        raise RuntimeError(f"Oomipood product JSON-LD is missing: {product_url}")

    offer = first_offer(product)
    availability = clean_text(offer.get("availability")).casefold()
    if availability and not availability.endswith("instock"):
        return None

    name = clean_text(product.get("name"))
    sku = clean_text(product.get("sku"))
    if not name or not sku:
        raise RuntimeError(f"Oomipood product has no name or SKU: {product_url}")

    current_price = parse_decimal_money(offer.get("price"))
    old_node = soup.select_one(".price-old")
    new_node = soup.select_one(".price-new")
    old_price = parse_price_text(old_node.get_text(" ")) if old_node else ""
    new_price = parse_price_text(new_node.get_text(" ")) if new_node else current_price
    if old_price != "" and new_price != "" and new_price < old_price:
        price, sale_price = old_price, new_price
    else:
        price, sale_price = current_price, ""
    if price == "":
        raise RuntimeError(f"Oomipood product has no public price: {product_url}")

    page_text = soup.get_text(" ", strip=True)
    ean_match = re.search(r"\bEAN\s*:\s*(\d{8}|\d{12,14})\b", page_text, re.IGNORECASE)
    barcode = normalize_barcode(ean_match.group(1)) if ean_match else ""

    body = soup.body
    body_classes = " ".join(body.get("class", [])) if body else ""
    product_id_match = re.search(r"\bproduct-product-(\d+)\b", body_classes)
    stable_id = (
        product_id_match.group(1)
        if product_id_match
        else hashlib.sha256(product_url.encode("utf-8")).hexdigest()[:24]
    )

    category_links = [
        link
        for link in soup.select(".breadcrumb a[href]")
        if "/category/" in clean_text(link.get("href"))
    ]
    category_link = category_links[-1] if category_links else None
    category_name = clean_text(category_link.get_text(" ")) if category_link else ""
    category_url = clean_text(category_link.get("href")) if category_link else ""
    category_id = (
        f"oomipood-category-{hashlib.sha256(category_url.encode('utf-8')).hexdigest()[:20]}"
        if category_url
        else ""
    )

    image = product.get("image")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl")

    return [
        name,
        price,
        sale_price,
        "",
        barcode,
        f"oomipood-{stable_id}",
        clean_text(image),
        product_url,
        sku,
        category_name,
        category_id,
        clean_text(product.get("description")),
    ]


async def fetch_rows(session: aiohttp.ClientSession, product_urls: list[str], log_callback=None):
    limiter = asyncio.Semaphore(CONCURRENCY)
    completed = 0
    missing = 0
    skipped = 0
    progress_lock = asyncio.Lock()

    async def fetch_one(index: int, product_url: str):
        nonlocal completed, missing, skipped
        try:
            async with limiter:
                html_text = await request_text(
                    session,
                    product_url,
                    label=f"product {index}/{len(product_urls)}",
                    log_callback=log_callback,
                )
            row = parse_product_page(html_text, product_url)
            if row is None:
                skipped += 1
            return row
        except ProductPageMissing:
            missing += 1
            return None
        finally:
            async with progress_lock:
                completed += 1
                if completed == len(product_urls) or completed % 250 == 0:
                    log(
                        f"Oomipood download progress: products={completed}/{len(product_urls)}, "
                        f"missing={missing}, unavailable={skipped}",
                        log_callback,
                    )

    rows = await asyncio.gather(
        *(fetch_one(index, url) for index, url in enumerate(product_urls, start=1))
    )
    available_rows = [row for row in rows if row is not None]
    if not available_rows:
        raise RuntimeError("Oomipood catalog contains no available products.")
    return available_rows, missing + skipped


async def main(output_path: str | Path, log_callback=None) -> None:
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, connect=15)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "et-EE,et;q=0.9,en;q=0.7",
        "User-Agent": "PriceCompareCatalogBot/1.0",
    }
    async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
        product_urls = await fetch_product_urls(session, log_callback)
        rows, skipped = await fetch_rows(session, product_urls, log_callback)

    rows.sort(key=lambda row: (str(row[0]).casefold(), str(row[5])))
    destination = Path(output_path)
    await asyncio.to_thread(save_excel, rows, destination)
    log(
        f"Oomipood Excel created: path={destination}, products={len(rows)}, skipped={skipped}",
        log_callback,
    )
