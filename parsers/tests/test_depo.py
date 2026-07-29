from decimal import Decimal
from unittest.mock import AsyncMock, patch

from django.test import TestCase

from catalog.models import PriceHistory, Product, ProductOffer, Shop
from parsers.models import ParserConfig, ParserRun
from parsers.services.runner import run_parser


def depo_product(product_id="101", name="DEPO drill", price=Decimal("12.99"), sale_price=None, barcode="EAN-101"):
    return {
        "id": product_id,
        "name": name,
        "price": price,
        "sale_price": sale_price,
        "barcode": barcode,
        "sku": product_id,
        "image_url": f"https://online.depo.ee/images/{product_id}.jpg",
        "product_url": f"https://online.depo.ee/product/{product_id}",
        "category_id": "7",
    }


class DepoParserTests(TestCase):
    def setUp(self):
        self.depo_shop = Shop.objects.create(name="DEPO", code="depo")
        self.depo_config = ParserConfig.objects.create(
            shop=self.depo_shop,
            name="DEPO parser",
            code="depo",
        )
        self.espak_shop = Shop.objects.create(name="ESPAK", code="espak")
        self.categories = [{"id": 7}]

    def run_with_products(self, products):
        with patch(
            "parsers.services.depo.DepoParser._fetch_remote_data",
            new=AsyncMock(return_value=(self.categories, products, [])),
        ):
            return run_parser("depo")

    def create_depo_offer(self, external_id="101", name="Old DEPO drill", is_active=True, is_available=True):
        product = Product.objects.create(name=name)
        return ProductOffer.objects.create(
            shop=self.depo_shop,
            product=product,
            external_id=external_id,
            sku=external_id,
            original_name=name,
            price=Decimal("10.00"),
            is_active=is_active,
            is_available=is_available,
        )

    def test_creates_new_offer(self):
        parser_run = self.run_with_products([depo_product()])

        self.assertEqual(parser_run.status, ParserRun.STATUS_SUCCESS)
        self.assertEqual(parser_run.products_created, 1)
        self.assertEqual(ProductOffer.objects.filter(shop=self.depo_shop).count(), 1)
        self.assertEqual(Product.objects.count(), 1)

    def test_updates_existing_offer(self):
        self.run_with_products([depo_product()])

        parser_run = self.run_with_products([depo_product(name="Updated DEPO drill")])

        self.assertEqual(parser_run.products_updated, 1)
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(ProductOffer.objects.get(shop=self.depo_shop, external_id="101").original_name, "Updated DEPO drill")

    def test_price_change_creates_price_history(self):
        self.run_with_products([depo_product(price=Decimal("12.99"))])

        parser_run = self.run_with_products([depo_product(price=Decimal("14.99"))])

        self.assertEqual(parser_run.prices_changed, 1)
        self.assertEqual(PriceHistory.objects.count(), 2)

    def test_same_price_does_not_create_price_history(self):
        self.run_with_products([depo_product(price=Decimal("12.99"))])

        parser_run = self.run_with_products([depo_product(price=Decimal("12.99"))])

        self.assertEqual(parser_run.prices_changed, 0)
        self.assertEqual(PriceHistory.objects.count(), 1)

    def test_safe_deactivation(self):
        self.run_with_products([depo_product(product_id="101")])

        parser_run = self.run_with_products([depo_product(product_id="202")])

        self.assertEqual(parser_run.status, ParserRun.STATUS_SUCCESS)
        old_offer = ProductOffer.objects.get(shop=self.depo_shop, external_id="101")
        self.assertFalse(old_offer.is_active)
        self.assertFalse(old_offer.is_available)

    def test_duplicates_are_not_saved_twice(self):
        parser_run = self.run_with_products([depo_product(product_id="101"), depo_product(product_id="101")])

        self.assertEqual(parser_run.products_found, 1)
        self.assertEqual(ProductOffer.objects.filter(shop=self.depo_shop).count(), 1)

    def test_network_error_fails_without_deactivation(self):
        self.create_depo_offer(external_id="101")

        with patch(
            "parsers.services.depo.DepoParser._fetch_remote_data",
            new=AsyncMock(side_effect=RuntimeError("network down")),
        ):
            parser_run = run_parser("depo")

        self.assertEqual(parser_run.status, ParserRun.STATUS_FAILED)
        offer = ProductOffer.objects.get(shop=self.depo_shop, external_id="101")
        self.assertTrue(offer.is_active)
        self.assertTrue(offer.is_available)

    def test_empty_catalog_fails_without_deactivation(self):
        self.create_depo_offer(external_id="101")

        parser_run = self.run_with_products([])

        self.assertEqual(parser_run.status, ParserRun.STATUS_FAILED)
        offer = ProductOffer.objects.get(shop=self.depo_shop, external_id="101")
        self.assertTrue(offer.is_active)
        self.assertTrue(offer.is_available)

    def test_small_catalog_fails_without_deactivation(self):
        for index in range(100):
            self.create_depo_offer(external_id=f"old-{index}")

        parser_run = self.run_with_products([depo_product(product_id=str(index)) for index in range(10)])

        self.assertEqual(parser_run.status, ParserRun.STATUS_FAILED)
        self.assertIn("anomalously small product list", parser_run.error_message)
        self.assertEqual(
            ProductOffer.objects.filter(shop=self.depo_shop, is_active=True, is_available=True).count(),
            100,
        )

    def test_espak_offers_are_not_touched(self):
        espak_product = Product.objects.create(name="ESPAK product")
        espak_offer = ProductOffer.objects.create(
            shop=self.espak_shop,
            product=espak_product,
            external_id="espak-1",
            original_name="ESPAK product",
            is_active=True,
            is_available=True,
        )

        self.run_with_products([depo_product(product_id="101")])

        espak_offer.refresh_from_db()
        self.assertTrue(espak_offer.is_active)
        self.assertTrue(espak_offer.is_available)
