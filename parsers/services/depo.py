import asyncio
from dataclasses import dataclass

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
from .depo_client import DEPO_WEBSITE_URL, DepoClient, clean_text
from .espak_client import stable_external_id


@dataclass
class ParsedDepoProduct:
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


class DepoParser(BaseStoreParser):
    code = "depo"
    DEACTIVATE_BATCH_SIZE = 1000
    MIN_ACTIVE_OFFERS_FOR_ANOMALY_CHECK = 100
    MIN_REMOTE_TO_ACTIVE_RATIO = 0.2

    def run(self):
        started_at = timezone.now()
        shop = self.parser_config.shop
        result = ParserResult()

        self.log("DEPO parser started.")
        raw_categories, raw_products, client_logs = self._run_async(self._fetch_remote_data())
        for message in client_logs:
            self.log(message)
        self.log(f"DEPO categories loaded: {len(raw_categories)}")
        self.log(f"DEPO raw products loaded: {len(raw_products)}")

        if not raw_products:
            raise ParserError("DEPO returned an empty product list; refusing to deactivate existing offers.")

        categories = self._save_categories(shop, raw_categories)
        unique_products = self._remove_duplicates(raw_products)
        result.products_found = len(unique_products)
        remote_external_ids = self._get_remote_external_ids(unique_products)
        self.log(f"DEPO unique products: {result.products_found}")
        self._validate_remote_catalog_size(shop, remote_external_ids)

        for index, raw_product in enumerate(unique_products, start=1):
            try:
                parsed = self._parse_product(raw_product)
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
                    self.log(f"DEPO progress: {index}/{result.products_found} products processed.")

            except Exception as exc:
                result.errors_count += 1
                self.log(f"DEPO product error: {exc}")

        self._deactivate_missing_offers(shop, remote_external_ids)
        self.log(
            "DEPO parser finished: found={found}, created={created}, updated={updated}, "
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
        async with DepoClient(log_callback=client_logs.append) as client:
            categories = await client.fetch_categories()
            products = await client.fetch_products(categories)
        return categories, products, client_logs

    def _run_async(self, coroutine):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)

        raise ParserError("DEPO parser cannot run inside an already running event loop.")

    def _save_categories(self, shop, raw_categories):
        saved_by_external_id = {}

        for raw_category in raw_categories:
            external_id = self._category_external_id(raw_category)
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
            saved_by_external_id[external_id] = category

        self.log(f"DEPO category sync finished: {len(saved_by_external_id)} categories.")
        return saved_by_external_id

    def _parse_product(self, product) -> ParsedDepoProduct:
        sale_price = product.get("sale_price")
        regular_price = product.get("price")

        if sale_price is not None:
            price = regular_price
        else:
            price = regular_price
            sale_price = None

        return ParsedDepoProduct(
            external_id=self._get_product_external_id(product),
            name=clean_text(product.get("name")),
            brand="",
            model="",
            barcode=clean_text(product.get("barcode")),
            sku=clean_text(product.get("sku")),
            description=clean_text(product.get("description")),
            price=price,
            sale_price=sale_price,
            product_url=clean_text(product.get("product_url")),
            image_url=clean_text(product.get("image_url")),
            is_available=True,
            category_external_id=clean_text(product.get("category_id")),
        )

    @transaction.atomic
    def _save_product_offer(self, *, shop, category, parsed: ParsedDepoProduct, seen_at):
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

    def _deactivate_missing_offers(self, shop, remote_external_ids):
        missing_offer_ids = [
            offer_id
            for offer_id, external_id in ProductOffer.objects.filter(shop=shop).values_list(
                "id",
                "external_id",
            )
            if external_id not in remote_external_ids
        ]

        updated = 0
        for start in range(0, len(missing_offer_ids), self.DEACTIVATE_BATCH_SIZE):
            batch = missing_offer_ids[start : start + self.DEACTIVATE_BATCH_SIZE]
            updated += ProductOffer.objects.filter(id__in=batch).update(
                is_active=False,
                is_available=False,
            )

        self.log(f"DEPO inactive offers marked: {updated}.")

    def _remove_duplicates(self, raw_products):
        unique = {}

        for product in raw_products:
            external_id = self._get_product_external_id(product)
            if external_id not in unique:
                unique[external_id] = product

        return list(unique.values())

    def _get_product_external_id(self, product):
        return clean_text(product.get("id")) or clean_text(product.get("sku")) or stable_external_id(
            product.get("product_url"),
            product.get("barcode"),
            product.get("name"),
        )

    def _get_remote_external_ids(self, products):
        external_ids = set()

        for product in products:
            external_id = self._get_product_external_id(product)
            if external_id:
                external_ids.add(external_id)

        return external_ids

    def _validate_remote_catalog_size(self, shop, remote_external_ids):
        active_count = ProductOffer.objects.filter(shop=shop, is_active=True).count()

        if active_count < self.MIN_ACTIVE_OFFERS_FOR_ANOMALY_CHECK:
            return

        minimum_expected = active_count * self.MIN_REMOTE_TO_ACTIVE_RATIO
        if len(remote_external_ids) >= minimum_expected:
            return

        message = (
            "DEPO returned an anomalously small product list: "
            f"{len(remote_external_ids)} remote products for {active_count} active offers. "
            "Refusing to deactivate existing offers."
        )
        self.log(message)
        raise ParserError(message)

    def _category_external_id(self, category):
        return clean_text(category.get("id")) or stable_external_id(category)

    def _category_name(self, category):
        external_id = self._category_external_id(category)
        return f"DEPO category {external_id}" if external_id else "DEPO"

    def _category_url(self, category):
        external_id = self._category_external_id(category)
        return f"{DEPO_WEBSITE_URL}/products/{external_id}" if external_id else DEPO_WEBSITE_URL
