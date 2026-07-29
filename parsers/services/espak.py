import asyncio
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from catalog.models import Category, PriceHistory, Product, ProductOffer
from catalog.services.normalization import (
    normalize_brand,
    normalize_model,
    normalize_product_name,
    normalize_text,
)

from .base import BaseStoreParser, ParserError, ParserResult
from .espak_client import (
    EspakClient,
    clean_text,
    get_attribute,
    parse_price,
    stable_external_id,
)


@dataclass
class ParsedEspakProduct:
    external_id: str
    name: str
    brand: str
    model: str
    barcode: str
    sku: str
    description: str
    price: object
    sale_price: object
    product_url: str
    image_url: str
    is_available: bool
    category_external_id: str


class EspakParser(BaseStoreParser):
    code = "espak"

    def run(self):
        started_at = timezone.now()
        shop = self.parser_config.shop
        result = ParserResult()

        self.log("ESPAK parser started.")
        raw_categories, raw_products, client_logs = self._run_async(self._fetch_remote_data())
        for message in client_logs:
            self.log(message)
        self.log(f"ESPAK categories loaded: {len(raw_categories)}")
        self.log(f"ESPAK raw products loaded: {len(raw_products)}")

        categories = self._save_categories(shop, raw_categories)
        unique_products = self._remove_duplicates(raw_products)
        result.products_found = len(unique_products)
        self.log(f"ESPAK unique products: {result.products_found}")

        for index, raw_product in enumerate(unique_products, start=1):
            try:
                parsed = self._parse_product(raw_product)
                self._ensure_product_category(shop, raw_product, categories)
                category = categories.get(parsed.category_external_id)
                created, price_changed = self._save_product_offer(
                    shop=shop,
                    category=category,
                    parsed=parsed,
                    seen_at=started_at,
                )

                if created:
                    result.products_created += 1
                else:
                    result.products_updated += 1

                if price_changed:
                    result.prices_changed += 1

                if index % 100 == 0:
                    self.log(f"ESPAK progress: {index}/{result.products_found} products processed.")

            except Exception as exc:
                result.errors_count += 1
                self.log(f"ESPAK product error: {exc}")

        self._deactivate_missing_offers(shop, started_at)
        self.log(
            "ESPAK parser finished: found={found}, created={created}, updated={updated}, "
            "prices_changed={prices_changed}, errors={errors}.".format(
                found=result.products_found,
                created=result.products_created,
                updated=result.products_updated,
                prices_changed=result.prices_changed,
                errors=result.errors_count,
            )
        )
        return result

    async def _fetch_remote_data(self):
        client_logs = []
        async with EspakClient(log_callback=client_logs.append) as client:
            categories = await client.fetch_categories()
            products = await client.fetch_products()
        return categories, products, client_logs

    def _run_async(self, coroutine):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)

        raise ParserError("ESPAK parser cannot run inside an already running event loop.")

    def _save_categories(self, shop, raw_categories):
        self.log("ESPAK category sync started.")
        saved_by_external_id = {}
        pending_parent_links = []

        for raw_category in raw_categories:
            external_id = self._category_external_id(raw_category)
            parent_external_id = self._category_parent_external_id(raw_category)
            defaults = {
                "name": self._category_name(raw_category),
                "normalized_name": normalize_text(self._category_name(raw_category)),
                "url": self._category_url(raw_category),
                "parent": None,
            }
            category, _ = Category.objects.update_or_create(
                shop=shop,
                external_id=external_id,
                defaults=defaults,
            )
            saved_by_external_id[external_id] = category

            if parent_external_id:
                pending_parent_links.append((category, parent_external_id))

        for category, parent_external_id in pending_parent_links:
            parent = saved_by_external_id.get(parent_external_id)
            if parent and category.parent_id != parent.pk:
                category.parent = parent
                category.save(update_fields=["parent", "updated_at"])

        self.log(f"ESPAK category sync finished: {len(saved_by_external_id)} categories.")
        return saved_by_external_id

    def _ensure_product_category(self, shop, raw_product, categories):
        raw_category = self._first_product_category(raw_product)
        if not raw_category:
            return None

        external_id = self._category_external_id(raw_category)
        if external_id in categories:
            return categories[external_id]

        category, _ = Category.objects.update_or_create(
            shop=shop,
            external_id=external_id,
            defaults={
                "name": self._category_name(raw_category),
                "normalized_name": normalize_text(self._category_name(raw_category)),
                "parent": None,
                "url": self._category_url(raw_category),
            },
        )
        categories[external_id] = category
        return category

    def _parse_product(self, product: dict[str, Any]) -> ParsedEspakProduct:
        prices = product.get("prices") or {}
        minor_unit = self._minor_unit(prices)
        regular_price = parse_price(prices.get("regular_price"), minor_unit)
        sale_price = parse_price(prices.get("sale_price"), minor_unit)

        if bool(product.get("on_sale")) and sale_price is not None:
            price = regular_price
        else:
            price = sale_price if sale_price is not None else regular_price
            sale_price = None

        category = self._first_product_category(product)
        name = clean_text(product.get("name"))
        external_id = clean_text(product.get("id")) or stable_external_id(
            product.get("permalink"),
            product.get("sku"),
            name,
        )

        return ParsedEspakProduct(
            external_id=external_id,
            name=name,
            brand=get_attribute(product, "Brand", "Kaubamärk", "Tootja", "Бренд"),
            model=get_attribute(product, "Mudel", "Model", "Модель"),
            barcode=get_attribute(product, "Ribakood", "EAN", "GTIN", "Barcode", "Штрихкод"),
            sku=clean_text(product.get("sku")),
            description=self._description(product),
            price=price,
            sale_price=sale_price,
            product_url=clean_text(product.get("permalink")),
            image_url=self._image_url(product),
            is_available=self._is_available(product),
            category_external_id=self._category_external_id(category) if category else "",
        )

    @transaction.atomic
    def _save_product_offer(self, *, shop, category, parsed: ParsedEspakProduct, seen_at):
        offer = (
            ProductOffer.objects.select_related("product")
            .filter(shop=shop, external_id=parsed.external_id)
            .first()
        )
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
        offer.currency = "EUR"
        offer.product_url = parsed.product_url
        offer.image_url = parsed.image_url
        offer.is_available = parsed.is_available
        offer.is_active = True
        offer.last_seen_at = seen_at
        offer.save()

        price_known = parsed.price is not None or parsed.sale_price is not None
        price_changed = (
            created
            and price_known
            or previous_price != parsed.price
            or previous_sale_price != parsed.sale_price
        )

        if price_changed:
            PriceHistory.objects.create(
                offer=offer,
                price=parsed.price,
                sale_price=parsed.sale_price,
            )

        return created, price_changed

    def _deactivate_missing_offers(self, shop, started_at):
        updated = ProductOffer.objects.filter(shop=shop).filter(
            Q(last_seen_at__lt=started_at) | Q(last_seen_at__isnull=True)
        ).update(is_active=False, is_available=False)
        self.log(f"ESPAK inactive offers marked: {updated}.")

    def _remove_duplicates(self, raw_products):
        unique = {}

        for product in raw_products:
            key = (
                product.get("id")
                or product.get("permalink")
                or product.get("sku")
                or product.get("name")
            )
            unique[key] = product

        return list(unique.values())

    def _category_external_id(self, category):
        if not isinstance(category, dict):
            return ""

        return clean_text(category.get("id")) or clean_text(category.get("slug")) or stable_external_id(
            category.get("permalink"),
            category.get("link"),
            category.get("name"),
        )

    def _category_parent_external_id(self, category):
        parent = category.get("parent") if isinstance(category, dict) else None
        if parent in (None, "", 0, "0"):
            return ""
        return clean_text(parent)

    def _category_name(self, category):
        return clean_text(category.get("name")) or "ESPAK"

    def _category_url(self, category):
        return clean_text(category.get("permalink")) or clean_text(category.get("link"))

    def _first_product_category(self, product):
        categories = product.get("categories") or []
        for category in categories:
            if isinstance(category, dict):
                return category
        return None

    def _minor_unit(self, prices):
        try:
            return int(prices.get("currency_minor_unit", 2) or 2)
        except (TypeError, ValueError):
            return 2

    def _description(self, product):
        description = clean_text(product.get("description"))
        short_description = clean_text(product.get("short_description"))
        return description or short_description

    def _image_url(self, product):
        for image in product.get("images") or []:
            if isinstance(image, dict) and image.get("src"):
                return clean_text(image.get("src"))
        return ""

    def _is_available(self, product):
        if "is_in_stock" in product:
            return bool(product.get("is_in_stock"))
        if product.get("add_to_cart") and isinstance(product["add_to_cart"], dict):
            return not bool(product["add_to_cart"].get("disabled"))
        return True
