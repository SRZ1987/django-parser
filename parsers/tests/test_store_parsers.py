import asyncio
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from catalog.models import Category, PriceHistory, Product, ProductOffer, Shop
from parsers.models import ParserConfig, ParserRun
from parsers.services.base import ParserError
from parsers.services.bauhaus import BAUHAUS_WEBSITE_URL, BauhausClient, BauhausParser, extract_hits_from_document, product_from_hit
from parsers.services.bauhof import BauhofParser, extract_product_from_url, normalize_product as normalize_bauhof_product
from parsers.services.ehituseabc import EhituseABCParser, normalize_record
from parsers.services.fere import FereParser, parse_product_fragment
from parsers.services.registry import PARSER_REGISTRY
from parsers.services.runner import run_parser
from parsers.services.store_catalog import HttpRequestError, StoreCategory, StoreProduct, parse_decimal


class StoreParserMixin:
    parser_class = None
    shop_code = ""
    shop_name = ""

    def setUp(self):
        self.shop = Shop.objects.create(name=self.shop_name, code=self.shop_code)
        self.config = ParserConfig.objects.create(
            shop=self.shop,
            name=f"{self.shop_name} parser",
            code=self.shop_code,
        )
        self.run = ParserRun.objects.create(parser=self.config, trigger=ParserRun.TRIGGER_COMMAND)

    def parser(self, categories=None, products=None, complete=True):
        parser = self.parser_class(self.config, self.run)

        async def fake_fetch():
            return categories or [], products or [], complete

        parser._fetch_remote_data = fake_fetch
        return parser

    def sample_product(self, external_id="sku-1", name="Bosch drill", price=Decimal("10.00"), sale_price=None):
        return StoreProduct(
            external_id=external_id,
            sku=external_id,
            barcode="4740000000001",
            name=name,
            brand="Bosch",
            model="GSR",
            category_external_id="tools",
            category_name="Tools",
            price=price,
            sale_price=sale_price,
            product_url="https://example.com/product",
            image_url="https://example.com/image.jpg",
        )

    def test_parser_is_registered(self):
        self.assertIs(PARSER_REGISTRY[self.shop_code], self.parser_class)

    def test_successful_run_creates_offer(self):
        result = self.parser(
            categories=[StoreCategory(external_id="tools", name="Tools")],
            products=[self.sample_product()],
        ).run()

        offer = ProductOffer.objects.get(shop=self.shop, external_id="sku-1")
        self.assertEqual(result.products_created, 1)
        self.assertEqual(offer.original_name, "Bosch drill")
        self.assertEqual(offer.category.name, "Tools")
        self.assertEqual(offer.price, Decimal("10.00"))
        self.assertTrue(offer.is_active)

    def test_repeat_run_updates_offer_without_duplicate(self):
        self.parser(products=[self.sample_product(price=Decimal("10.00"))]).run()
        self.parser(products=[self.sample_product(name="Bosch drill updated", price=Decimal("12.00"))]).run()

        self.assertEqual(ProductOffer.objects.filter(shop=self.shop).count(), 1)
        offer = ProductOffer.objects.get(shop=self.shop, external_id="sku-1")
        self.assertEqual(offer.original_name, "Bosch drill updated")
        self.assertEqual(offer.price, Decimal("12.00"))

    def test_price_change_creates_price_history(self):
        self.parser(products=[self.sample_product(price=Decimal("10.00"))]).run()
        self.parser(products=[self.sample_product(price=Decimal("11.00"))]).run()

        offer = ProductOffer.objects.get(shop=self.shop, external_id="sku-1")
        self.assertEqual(PriceHistory.objects.filter(offer=offer).count(), 2)

    def test_product_error_does_not_break_whole_run(self):
        result = self.parser(products=[self.sample_product(external_id="", name="Broken")]).run()

        self.assertEqual(result.errors_count, 0)
        self.assertEqual(ProductOffer.objects.count(), 0)

    def test_full_load_deactivates_missing_offer(self):
        product = Product.objects.create(name="Old product")
        old_offer = ProductOffer.objects.create(
            shop=self.shop,
            product=product,
            external_id="old",
            original_name="Old product",
            is_active=True,
            is_available=True,
        )

        self.parser(products=[self.sample_product(external_id="new")]).run()

        old_offer.refresh_from_db()
        self.assertFalse(old_offer.is_active)
        self.assertFalse(old_offer.is_available)

    def test_incomplete_load_does_not_deactivate(self):
        product = Product.objects.create(name="Old product")
        old_offer = ProductOffer.objects.create(
            shop=self.shop,
            product=product,
            external_id="old",
            original_name="Old product",
            is_active=True,
            is_available=True,
        )

        with self.assertRaises(ParserError):
            self.parser(products=[self.sample_product(external_id="new")], complete=False).run()

        old_offer.refresh_from_db()
        self.assertTrue(old_offer.is_active)

    def test_empty_catalog_does_not_deactivate(self):
        product = Product.objects.create(name="Old product")
        old_offer = ProductOffer.objects.create(
            shop=self.shop,
            product=product,
            external_id="old",
            original_name="Old product",
            is_active=True,
            is_available=True,
        )

        with self.assertRaises(ParserError):
            self.parser(products=[]).run()

        old_offer.refresh_from_db()
        self.assertTrue(old_offer.is_active)

    def test_runner_clears_is_running_after_error(self):
        self.config.is_running = False
        self.config.save(update_fields=["is_running"])

        async def failing_fetch(parser_self):
            raise ParserError("network failed")

        with patch.object(self.parser_class, "_fetch_remote_data", failing_fetch):
            result = run_parser(self.shop_code)

        self.config.refresh_from_db()
        self.assertEqual(result.status, ParserRun.STATUS_FAILED)
        self.assertFalse(self.config.is_running)

    def test_runner_clears_is_running_after_cancelled_error(self):
        self.config.is_running = False
        self.config.save(update_fields=["is_running"])

        async def cancelled_fetch(parser_self):
            raise asyncio.CancelledError()

        with patch.object(self.parser_class, "_fetch_remote_data", cancelled_fetch):
            with self.assertRaises(asyncio.CancelledError):
                run_parser(self.shop_code)

        self.config.refresh_from_db()
        self.assertFalse(self.config.is_running)


class BauhofParserTests(StoreParserMixin, TestCase):
    parser_class = BauhofParser
    shop_code = "bauhof"
    shop_name = "Bauhof"

    def test_extracts_sku_from_sitemap_url(self):
        self.assertEqual(
            extract_product_from_url("https://www.bauhof.ee/et/p/12345/product-name"),
            ("12345", "https://www.bauhof.ee/et/p/12345/product-name"),
        )

    def test_normalizes_api_product(self):
        product = normalize_bauhof_product(
            {
                "sku": "SKU-1",
                "name": "Bauhof hammer",
                "barcode": "4740000000001.0",
                "price_range": {
                    "minimum_price": {
                        "regular_price": {"value": "12.50"},
                        "final_price": {"value": "9.99"},
                    }
                },
                "thumbnail": {"url": "/image.jpg"},
            },
            "https://www.bauhof.ee/et/p/SKU-1/hammer",
        )

        self.assertEqual(product.external_id, "SKU-1")
        self.assertEqual(product.name, "Bauhof hammer")
        self.assertEqual(product.price, Decimal("12.50"))
        self.assertEqual(product.sale_price, Decimal("9.99"))
        self.assertEqual(product.barcode, "4740000000001")
        self.assertEqual(product.product_url, "https://www.bauhof.ee/et/p/SKU-1/hammer")


class EhituseABCParserTests(StoreParserMixin, TestCase):
    parser_class = EhituseABCParser
    shop_code = "ehituseabc"
    shop_name = "Ehituse ABC"

    def test_normalizes_klevu_record(self):
        product = normalize_record(
            {
                "sku": "ABC-1",
                "name": "Ehituse paint",
                "price": "14,90",
                "salePrice": "10.00",
                "ean": " 474-000 ",
                "url": "/product",
                "image": "/image.jpg",
            }
        )

        self.assertEqual(product.external_id, "ABC-1")
        self.assertEqual(product.price, Decimal("14.90"))
        self.assertEqual(product.sale_price, Decimal("10.00"))
        self.assertEqual(product.product_url, "https://www.ehituseabc.ee/product")
        self.assertEqual(product.image_url, "https://www.ehituseabc.ee/image.jpg")


class FereParserTests(StoreParserMixin, TestCase):
    parser_class = FereParser
    shop_code = "fere"
    shop_name = "FERE"

    def test_parses_product_fragment(self):
        product = parse_product_fragment(
            """
            <h2 class="product-name"><a href="/tool.html">Fere tool</a></h2>
            <strong class="product-code">Tootekood: FERE-1</strong>
            <span class="product-ean">EAN: 4740000000002</span>
            <span class="regular-price"><span class="price">12,00 €</span></span>
            <span class="special-price"><span class="price">9,00 €</span></span>
            <img src="/image.jpg">
            """,
            StoreCategory(external_id="cat", name="Tools", url="https://fere.ee/tools.html"),
        )

        self.assertEqual(product.external_id, "FERE-1")
        self.assertEqual(product.name, "Fere tool")
        self.assertEqual(product.barcode, "4740000000002")
        self.assertEqual(product.price, Decimal("12.00"))
        self.assertEqual(product.sale_price, Decimal("9.00"))
        self.assertEqual(product.product_url, "https://fere.ee/tool.html")


class BauhausParserTests(StoreParserMixin, TestCase):
    parser_class = BauhausParser
    shop_code = "bauhaus"
    shop_name = "BAUHAUS"

    def test_extracts_hits_from_next_document(self):
        document = 'self.__next_f.push([1, "{\\"hits\\":[{\\"sku\\":\\"BH-1\\",\\"name\\":\\"Bauhaus item\\",\\"url\\":\\"/item\\"}]}"])'

        self.assertEqual(extract_hits_from_document(document)[0]["sku"], "BH-1")

    def test_normalizes_hit_product(self):
        product = product_from_hit(
            {
                "sku": "BH-1",
                "name": "Bauhaus item",
                "brand_name": "Brand",
                "ean": "4740000000003",
                "url": "/item",
                "image_url": "/image.jpg",
                "bauhaus_price": {
                    "ordinary_price": {"value": "20.00"},
                    "final_price": {"value": "15.00", "currency": "EUR"},
                },
                "categories": {"level1": ["Tools /// Drills"]},
            },
            StoreCategory(external_id="cat", name="Tools", url="https://www.bauhaus.ee/tools"),
        )

        self.assertEqual(product.external_id, "BH-1")
        self.assertEqual(product.price, Decimal("20.00"))
        self.assertEqual(product.sale_price, Decimal("15.00"))
        self.assertEqual(product.barcode, "4740000000003")
        self.assertEqual(product.product_url, "https://www.bauhaus.ee/item")

    def test_home_429_falls_back_to_alternative_category_source(self):
        calls = []
        category_document = '<a href="/tooriistad/akutrellid">Akutrellid</a>'

        async def fake_entry_text(client_self, url):
            calls.append(url)
            if url == BAUHAUS_WEBSITE_URL:
                raise HttpRequestError("HTTP 429", status=429, retryable=True)
            return category_document

        async def fake_fetch_category(client_self, category):
            return [
                StoreProduct(
                    external_id="BH-1",
                    sku="BH-1",
                    name="Bauhaus drill",
                    price=Decimal("20.00"),
                    product_url="https://www.bauhaus.ee/bh-1",
                )
            ]

        with patch("parsers.services.bauhaus.BAUHAUS_ENTRYPOINTS", (BAUHAUS_WEBSITE_URL, f"{BAUHAUS_WEBSITE_URL}/sitemap.xml")):
            with patch.object(BauhausClient, "request_entry_text", fake_entry_text):
                with patch.object(BauhausClient, "fetch_category", fake_fetch_category):
                    client = BauhausClient(log_callback=lambda message: None)
                    categories, products, complete = asyncio.run(client.fetch_products())

        self.assertTrue(complete)
        self.assertEqual(calls, [BAUHAUS_WEBSITE_URL, f"{BAUHAUS_WEBSITE_URL}/sitemap.xml"])
        self.assertEqual(len(categories), 1)
        self.assertEqual(products[0].external_id, "BH-1")

    def test_entrypoint_429_is_not_retried(self):
        calls = []

        async def always_429(client_self, url):
            calls.append(url)
            raise HttpRequestError("HTTP 429", status=429, retryable=True)

        with patch("parsers.services.bauhaus.BAUHAUS_ENTRYPOINTS", (BAUHAUS_WEBSITE_URL,)):
            with patch.object(BauhausClient, "request_entry_text", always_429):
                client = BauhausClient(log_callback=lambda message: None)
                with self.assertRaises(ValueError):
                    asyncio.run(client.fetch_entry_document())

        self.assertEqual(calls, [BAUHAUS_WEBSITE_URL])

    def test_bauhaus_failed_fetch_does_not_deactivate_old_offers(self):
        product = Product.objects.create(name="Old BAUHAUS product")
        old_offer = ProductOffer.objects.create(
            shop=self.shop,
            product=product,
            external_id="old-bh",
            original_name="Old BAUHAUS product",
            is_active=True,
            is_available=True,
        )

        async def failing_fetch(parser_self):
            raise ParserError("BAUHAUS category source is unavailable")

        with patch.object(BauhausParser, "_fetch_remote_data", failing_fetch):
            result = run_parser("bauhaus")

        old_offer.refresh_from_db()
        self.assertEqual(result.status, ParserRun.STATUS_FAILED)
        self.assertTrue(old_offer.is_active)
        self.assertTrue(old_offer.is_available)


class StoreCatalogUtilityTests(TestCase):
    def test_parse_decimal_handles_empty_and_text_values(self):
        self.assertIsNone(parse_decimal(""))
        self.assertIsNone(parse_decimal("call us"))
        self.assertEqual(parse_decimal("2,49 €"), Decimal("2.49"))
