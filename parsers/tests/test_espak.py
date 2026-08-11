from decimal import Decimal
from unittest.mock import AsyncMock, patch

from django.test import TestCase

from catalog.models import PriceHistory, Product, ProductOffer, Shop
from parsers.models import ParserConfig, ParserRun
from parsers.services.espak_client import parse_price
from parsers.services.espak import EspakParser
from parsers.services.runner import run_parser
from parsers.standalone import espak_parser as standalone_espak_parser


def espak_product(product_id=101, name="Bosch GSR 18V-50", price="1299", sale_price="", on_sale=False):
    return {
        "id": product_id,
        "name": name,
        "sku": f"SKU-{product_id}",
        "permalink": f"https://espak.ee/epood/product/{product_id}/",
        "description": "<p>Reliable drill</p>",
        "short_description": "",
        "on_sale": on_sale,
        "is_in_stock": True,
        "prices": {
            "regular_price": price,
            "sale_price": sale_price,
            "currency_minor_unit": 2,
        },
        "images": [{"src": f"https://espak.ee/images/{product_id}.jpg"}],
        "attributes": [
            {"name": "Ribakood", "terms": [{"name": f"EAN-{product_id}"}]},
            {"name": "Kaubamärk", "terms": [{"name": "Bosch"}]},
            {"name": "Mudel", "terms": [{"name": "GSR 18V-50"}]},
        ],
        "categories": [{"id": 7, "name": "Tools", "slug": "tools"}],
    }


class EspakPriceTests(TestCase):
    def test_parse_price(self):
        self.assertEqual(parse_price("12,99 €"), Decimal("12.99"))
        self.assertEqual(parse_price("12.99"), Decimal("12.99"))
        self.assertIsNone(parse_price(""))
        self.assertIsNone(parse_price("not a price"))

    def test_excel_row_preserves_catalog_metadata(self):
        row = standalone_espak_parser.parse_product(espak_product())

        self.assertEqual(row["SKU"], "SKU-101")
        self.assertEqual(row["Category"], "Tools")
        self.assertEqual(row["Category ID"], "7")
        self.assertEqual(row["Description"], "Reliable drill")
        self.assertEqual(row["Brand"], "Bosch")
        self.assertEqual(row["Model"], "GSR 18V-50")


class EspakParserTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="ESPAK", code="espak")
        self.parser_config = ParserConfig.objects.create(
            shop=self.shop,
            name="ESPAK parser",
            code="espak",
        )
        self.categories = [{"id": 7, "name": "Tools", "slug": "tools"}]

    def run_with_products(self, products):
        with patch(
            "parsers.services.espak.EspakParser._fetch_remote_data",
            new=AsyncMock(return_value=(self.categories, products, [])),
        ):
            return run_parser("espak")

    def create_offer(self, external_id="101", is_active=True, is_available=True):
        product = Product.objects.create(name=f"Product {external_id}")
        return ProductOffer.objects.create(
            shop=self.shop,
            product=product,
            external_id=external_id,
            original_name=f"Product {external_id}",
            price=Decimal("12.99"),
            is_active=is_active,
            is_available=is_available,
        )

    def test_creates_new_product_offer_and_price_history(self):
        parser_run = self.run_with_products([espak_product()])

        self.assertEqual(parser_run.status, ParserRun.STATUS_SUCCESS)
        self.assertEqual(parser_run.products_created, 1)
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(ProductOffer.objects.count(), 1)
        self.assertEqual(PriceHistory.objects.count(), 1)

    def test_updates_existing_offer_without_creating_product(self):
        self.run_with_products([espak_product()])

        parser_run = self.run_with_products([espak_product(name="Bosch updated")])

        self.assertEqual(parser_run.products_updated, 1)
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(ProductOffer.objects.get(external_id="101").original_name, "Bosch updated")

    def test_creates_price_history_when_price_changes(self):
        self.run_with_products([espak_product(price="1299")])

        parser_run = self.run_with_products([espak_product(price="1499")])

        self.assertEqual(parser_run.prices_changed, 1)
        self.assertEqual(PriceHistory.objects.count(), 2)

    def test_does_not_create_price_history_when_price_is_same(self):
        self.run_with_products([espak_product(price="1299")])

        parser_run = self.run_with_products([espak_product(price="1299")])

        self.assertEqual(parser_run.prices_changed, 0)
        self.assertEqual(PriceHistory.objects.count(), 1)

    def test_missing_offer_is_deactivated_after_successful_full_run(self):
        self.run_with_products([espak_product(product_id=101)])

        parser_run = self.run_with_products([espak_product(product_id=202)])

        self.assertEqual(parser_run.status, ParserRun.STATUS_SUCCESS)
        old_offer = ProductOffer.objects.get(external_id="101")
        self.assertFalse(old_offer.is_active)
        self.assertFalse(old_offer.is_available)

    def test_old_offer_is_not_deactivated_after_failed_run(self):
        self.run_with_products([espak_product(product_id=101)])

        with patch(
            "parsers.services.espak.EspakParser._fetch_remote_data",
            new=AsyncMock(side_effect=RuntimeError("network down")),
        ):
            parser_run = run_parser("espak")

        self.assertEqual(parser_run.status, ParserRun.STATUS_FAILED)
        old_offer = ProductOffer.objects.get(external_id="101")
        self.assertTrue(old_offer.is_active)
        self.assertTrue(old_offer.is_available)

    def test_existing_remote_product_with_processing_error_stays_active(self):
        self.create_offer(external_id="101")

        with patch(
            "parsers.services.espak.EspakParser._fetch_remote_data",
            new=AsyncMock(return_value=(self.categories, [espak_product(product_id=101)], [])),
        ), patch(
            "parsers.services.espak.EspakParser._save_product_offer",
            side_effect=RuntimeError("bad product"),
        ):
            parser_run = run_parser("espak")

        offer = ProductOffer.objects.get(external_id="101")
        self.assertEqual(parser_run.status, ParserRun.STATUS_SUCCESS)
        self.assertEqual(parser_run.errors_count, 1)
        self.assertTrue(offer.is_active)
        self.assertTrue(offer.is_available)

    def test_empty_remote_products_fail_without_deactivation(self):
        self.create_offer(external_id="101")

        parser_run = self.run_with_products([])

        offer = ProductOffer.objects.get(external_id="101")
        self.assertEqual(parser_run.status, ParserRun.STATUS_FAILED)
        self.assertIn("empty product list", parser_run.error_message)
        self.assertTrue(offer.is_active)
        self.assertTrue(offer.is_available)

    def test_anomalously_small_result_fails_without_deactivation(self):
        for index in range(100):
            self.create_offer(external_id=f"old-{index}")

        products = [espak_product(product_id=index) for index in range(10)]
        parser_run = self.run_with_products(products)

        self.assertEqual(parser_run.status, ParserRun.STATUS_FAILED)
        self.assertIn("anomalously small product list", parser_run.error_message)
        self.assertEqual(
            ProductOffer.objects.filter(shop=self.shop, is_active=True, is_available=True).count(),
            100,
        )

    def test_same_external_id_function_is_used_for_deduplication_and_save(self):
        product = espak_product(product_id=None)
        product["id"] = None
        duplicate = dict(product)
        expected_external_id = EspakParser(self.parser_config, None)._get_product_external_id(product)

        parser_run = self.run_with_products([product, duplicate])

        self.assertEqual(parser_run.products_found, 1)
        self.assertEqual(ProductOffer.objects.count(), 1)
        self.assertEqual(ProductOffer.objects.get().external_id, expected_external_id)
