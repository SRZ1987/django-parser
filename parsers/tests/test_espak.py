from decimal import Decimal
from unittest.mock import AsyncMock, patch

from django.test import TestCase

from catalog.models import PriceHistory, Product, ProductOffer, Shop
from parsers.models import ParserConfig, ParserRun
from parsers.services.espak_client import parse_price
from parsers.services.runner import run_parser


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
