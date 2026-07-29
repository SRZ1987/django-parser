from django.core.management.base import BaseCommand

from catalog.models import Category, Product, ProductOffer
from catalog.services.normalization import (
    build_search_text,
    normalize_brand,
    normalize_model,
    normalize_product_name,
    normalize_text,
)


class Command(BaseCommand):
    help = "Recalculate normalized fields for catalog data."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=500)

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if batch_size <= 0:
            raise ValueError("--batch-size must be greater than zero.")

        category_count = self._normalize_categories(batch_size)
        product_count = self._normalize_products(batch_size)
        offer_count = self._normalize_offers(batch_size)

        self.stdout.write(
            self.style.SUCCESS(
                "Processed categories: {categories}, products: {products}, offers: {offers}".format(
                    categories=category_count,
                    products=product_count,
                    offers=offer_count,
                )
            )
        )

    def _normalize_categories(self, batch_size):
        processed = 0
        batch = []

        for category in Category.objects.iterator(chunk_size=batch_size):
            category.normalized_name = normalize_text(category.name)
            batch.append(category)
            processed += 1

            if len(batch) >= batch_size:
                Category.objects.bulk_update(batch, ["normalized_name"], batch_size=batch_size)
                batch.clear()

        if batch:
            Category.objects.bulk_update(batch, ["normalized_name"], batch_size=batch_size)

        return processed

    def _normalize_products(self, batch_size):
        processed = 0
        batch = []

        for product in Product.objects.iterator(chunk_size=batch_size):
            product.normalized_name = normalize_product_name(product.name)
            product.normalized_brand = normalize_brand(product.brand)
            product.normalized_model = normalize_model(product.model)
            batch.append(product)
            processed += 1

            if len(batch) >= batch_size:
                Product.objects.bulk_update(
                    batch,
                    ["normalized_name", "normalized_brand", "normalized_model"],
                    batch_size=batch_size,
                )
                batch.clear()

        if batch:
            Product.objects.bulk_update(
                batch,
                ["normalized_name", "normalized_brand", "normalized_model"],
                batch_size=batch_size,
            )

        return processed

    def _normalize_offers(self, batch_size):
        processed = 0
        batch = []
        queryset = ProductOffer.objects.select_related("shop", "product", "category")

        for offer in queryset.iterator(chunk_size=batch_size):
            offer.normalized_name = normalize_product_name(offer.original_name)
            offer.search_text = build_search_text(
                offer.original_name,
                offer.normalized_name,
                offer.sku,
                offer.barcode,
                offer.product.brand,
                offer.product.model,
                offer.category.name if offer.category else "",
                offer.shop.name,
            )
            batch.append(offer)
            processed += 1

            if len(batch) >= batch_size:
                ProductOffer.objects.bulk_update(
                    batch,
                    ["normalized_name", "search_text"],
                    batch_size=batch_size,
                )
                batch.clear()

        if batch:
            ProductOffer.objects.bulk_update(
                batch,
                ["normalized_name", "search_text"],
                batch_size=batch_size,
            )

        return processed
