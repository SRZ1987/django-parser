import asyncio
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from catalog.models import Category, PriceHistory, Product, ProductOffer, Shop
from parsers.models import ParserConfig, ParserRun
from parsers.services.base import ParserError
from parsers.services.bauhaus import (
    AdjustableLimiter,
    AdaptiveLoadController,
    BAUHAUS_WEBSITE_URL,
    BauhausClient,
    BauhausParser,
    apply_existing_barcodes,
    category_urls_from_tree,
    discover_category_tree,
    extract_catalog_metadata,
    extract_category_tree,
    enrich_all_products_with_ean,
    extract_ean_from_jsonld,
    extract_ean_from_next_data,
    extract_hits_from_document,
    fetch_product_ean,
    product_from_hit,
)
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

    def test_extracts_category_tree_from_next_document(self):
        document = (
            'self.__next_f.push([1, "{\\"categories\\":[{'
            '\\"name\\":\\"Tööriistad\\",\\"url_path\\":\\"tooriistad\\",'
            '\\"children\\":[{\\"name\\":\\"Akutrellid\\",\\"url_path\\":\\"tooriistad/akutrellid\\",\\"children\\":[]}]'
            '}]}"])'
        )

        tree = extract_category_tree(document)

        self.assertEqual(tree[0]["url_path"], "tooriistad")

    def test_category_tree_uses_recursive_leaf_categories_and_excludes_service_roots(self):
        tree = [
            {
                "name": "Tööriistad",
                "url_path": "tooriistad",
                "children": [
                    {
                        "name": "Akutrellid",
                        "url_path": "tooriistad/akutrellid",
                        "children": [
                            {
                                "name": "Makita",
                                "url_path": "tooriistad/akutrellid/makita",
                                "children": [],
                            }
                        ],
                    }
                ],
            },
            {
                "name": "Blog",
                "url_path": "blog",
                "children": [{"name": "Post", "url_path": "blog/post", "children": []}],
            },
        ]

        root_count, leaf_categories, category_audit = category_urls_from_tree(tree)

        self.assertEqual(root_count, 1)
        self.assertEqual(len(leaf_categories), 1)
        self.assertEqual(leaf_categories[0].url, "https://www.bauhaus.ee/tooriistad/akutrellid/makita")
        self.assertEqual(len(category_audit), 3)

    def test_rsc_fallback_extracts_category_tree_when_html_has_no_categories(self):
        class FakeHttpClient:
            def __init__(self):
                self.logs = []
                self.urls = []

            async def log(self, message):
                self.logs.append(message)

            async def get_text(self, url, **kwargs):
                self.urls.append((url, kwargs))
                return (
                    'self.__next_f.push([1, "{\\"categories\\":[{'
                    '\\"name\\":\\"Aed\\",\\"url_path\\":\\"aed\\",'
                    '\\"children\\":[{\\"name\\":\\"Grillid\\",\\"url_path\\":\\"aed/grillid\\",\\"children\\":[]}]'
                    '}]}"])'
                )

        http_client = FakeHttpClient()
        tree, source = asyncio.run(discover_category_tree(http_client, "<html>dpl_test</html>"))

        self.assertEqual(source, "RSC")
        self.assertEqual(tree[0]["url_path"], "aed")
        self.assertEqual(http_client.urls[0][1]["headers"]["rsc"], "1")
        self.assertEqual(http_client.urls[0][1]["headers"]["x-deployment-id"], "dpl_test")

    def test_extracts_hits_metadata_from_next_document(self):
        document = (
            'self.__next_f.push([1, "{\\"nbPages\\":3,\\"nbHits\\":82,\\"hitsPerPage\\":40,'
            '\\"hits\\":[{\\"sku\\":\\"BH-2\\",\\"name\\":\\"Bauhaus saw\\",\\"url\\":\\"/saw\\"}]}"])'
        )

        self.assertEqual(extract_catalog_metadata(document), {"nb_pages": 3, "nb_hits": 82, "hits_per_page": 40})
        self.assertEqual(extract_hits_from_document(document)[0]["sku"], "BH-2")

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

    def test_extracts_ean_from_jsonld_with_matching_sku(self):
        page_html = """
        <script type="application/ld+json">
        {
            "@type": "Product",
            "sku": "BH-1",
            "gtin13": "4006381333931"
        }
        </script>
        """

        self.assertEqual(extract_ean_from_jsonld(page_html, expected_sku="BH-1"), "4006381333931")

    def test_extracts_ean_from_matching_product_when_jsonld_has_multiple_products(self):
        page_html = """
        <script type="application/ld+json">
        [
            {"@type": "Product", "sku": "OTHER", "gtin13": "9780201379624"},
            {"@type": "Product", "sku": "BH-1", "gtin13": "4006381333931"}
        ]
        </script>
        """

        self.assertEqual(extract_ean_from_jsonld(page_html, expected_sku="BH-1"), "4006381333931")

    def test_extracts_ean_from_next_data_fallback(self):
        page_html = '<script>self.__next_f.push([1, "{\\"gtin13\\":\\"4006381333931\\"}"])</script>'

        self.assertEqual(extract_ean_from_next_data(page_html), "4006381333931")

    def test_fetch_product_ean_reads_product_page_jsonld(self):
        class FakeResponse:
            status_code = 200
            text = '<script type="application/ld+json">{"@type":"Product","sku":"BH-1","gtin13":"4006381333931"}</script>'
            headers = {}
            url = "https://www.bauhaus.ee/bh-1"

        class FakeSession:
            async def get(self, *args, **kwargs):
                return FakeResponse()

        limiter = AdjustableLimiter(1)
        controller = AdaptiveLoadController(limiter)
        _, ean, source, _ = asyncio.run(
            fetch_product_ean(FakeSession(), limiter, controller, "BH-1", "https://www.bauhaus.ee/bh-1")
        )

        self.assertEqual(ean, "4006381333931")
        self.assertEqual(source, "jsonld_gtin")

    def test_fetch_product_ean_retries_http_429_and_reads_second_response(self):
        class FakeResponse:
            def __init__(self, status_code, text="", headers=None):
                self.status_code = status_code
                self.text = text
                self.headers = headers or {}
                self.url = "https://www.bauhaus.ee/bh-1"

        class FakeSession:
            def __init__(self):
                self.responses = [
                    FakeResponse(429, headers={"Retry-After": "0"}),
                    FakeResponse(
                        200,
                        '<script type="application/ld+json">{"@type":"Product","sku":"BH-1","gtin13":"4006381333931"}</script>',
                    ),
                ]

            async def get(self, *args, **kwargs):
                return self.responses.pop(0)

        async def fake_sleep(*args, **kwargs):
            return None

        limiter = AdjustableLimiter(1)
        controller = AdaptiveLoadController(limiter)
        with patch("parsers.services.bauhaus.asyncio.sleep", fake_sleep):
            _, ean, source, _ = asyncio.run(
                fetch_product_ean(FakeSession(), limiter, controller, "BH-1", "https://www.bauhaus.ee/bh-1")
            )

        self.assertEqual(ean, "4006381333931")
        self.assertEqual(source, "jsonld_gtin")
        self.assertEqual(controller.restrictions, 1)

    def test_fetch_product_ean_retries_incomplete_http_200_before_not_found(self):
        class FakeResponse:
            def __init__(self, text):
                self.status_code = 200
                self.text = text
                self.headers = {}
                self.url = "https://www.bauhaus.ee/bh-1"

        class FakeSession:
            def __init__(self):
                self.responses = [
                    FakeResponse("<html>Security checkpoint</html>"),
                    FakeResponse(
                        '<script type="application/ld+json">{"@type":"Product","sku":"BH-1","gtin13":"4006381333931"}</script>'
                    ),
                ]

            async def get(self, *args, **kwargs):
                return self.responses.pop(0)

        async def fake_sleep(*args, **kwargs):
            return None

        limiter = AdjustableLimiter(1)
        controller = AdaptiveLoadController(limiter)
        with patch("parsers.services.bauhaus.asyncio.sleep", fake_sleep):
            _, ean, source, _ = asyncio.run(
                fetch_product_ean(FakeSession(), limiter, controller, "BH-1", "https://www.bauhaus.ee/bh-1")
            )

        self.assertEqual(ean, "4006381333931")
        self.assertEqual(source, "jsonld_gtin")

    def test_fetch_product_ean_returns_real_not_found_only_for_normal_product_page(self):
        class FakeResponse:
            status_code = 200
            text = '<script type="application/ld+json">{"@type":"Product","sku":"BH-1","name":"Bauhaus item"}</script>'
            headers = {}
            url = "https://www.bauhaus.ee/bh-1"

        class FakeSession:
            async def get(self, *args, **kwargs):
                return FakeResponse()

        limiter = AdjustableLimiter(1)
        controller = AdaptiveLoadController(limiter)
        _, ean, source, _ = asyncio.run(
            fetch_product_ean(FakeSession(), limiter, controller, "BH-1", "https://www.bauhaus.ee/bh-1")
        )

        self.assertEqual(ean, "")
        self.assertEqual(source, "real_not_found")

    def test_enrich_all_products_with_ean_sets_store_product_barcode(self):
        class FakeResponse:
            status_code = 200
            text = '<script type="application/ld+json">{"@type":"Product","sku":"BH-1","gtin13":"4006381333931"}</script>'
            headers = {}
            url = "https://www.bauhaus.ee/bh-1"

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, *args, **kwargs):
                return FakeResponse()

        def fake_session_factory(*args, **kwargs):
            return FakeSession()

        product = StoreProduct(
            external_id="BH-1",
            sku="BH-1",
            name="Bauhaus drill",
            product_url="https://www.bauhaus.ee/bh-1",
        )

        with patch("parsers.services.bauhaus.CurlAsyncSession", fake_session_factory):
            stats = asyncio.run(enrich_all_products_with_ean([product]))

        self.assertEqual(product.barcode, "4006381333931")
        self.assertEqual(stats["found"], 1)

    def test_enrich_all_products_with_ean_skips_existing_barcode(self):
        product = StoreProduct(
            external_id="BH-1",
            sku="BH-1",
            barcode="4006381333931",
            name="Bauhaus drill",
            product_url="https://www.bauhaus.ee/bh-1",
        )

        stats = asyncio.run(enrich_all_products_with_ean([product]))

        self.assertEqual(product.barcode, "4006381333931")
        self.assertEqual(stats["scheduled"], 0)

    def test_apply_existing_barcodes_preserves_database_barcode_before_enrichment(self):
        product = StoreProduct(
            external_id="BH-1",
            sku="BH-1",
            barcode="",
            name="Bauhaus drill",
            product_url="https://www.bauhaus.ee/bh-1",
        )

        apply_existing_barcodes([product], {"BH-1": "4006381333931"})

        self.assertEqual(product.barcode, "4006381333931")

    def test_curl_cffi_client_raises_429_without_repeating_blocked_request(self):
        class FakeResponse:
            status_code = 429
            text = "Vercel Security Checkpoint"
            headers = {"Retry-After": "7"}

        class FakeSession:
            def __init__(self):
                self.calls = 0

            async def get(self, *args, **kwargs):
                self.calls += 1
                return FakeResponse()

        client = __import__("parsers.services.bauhaus", fromlist=["BauhausHttpClient"]).BauhausHttpClient()
        client.session = FakeSession()

        with self.assertRaises(HttpRequestError):
            asyncio.run(client.get_text(BAUHAUS_WEBSITE_URL, endpoint_name="homepage"))

        self.assertEqual(client.session.calls, 1)

    def test_limited_client_processes_one_category_with_curl_transport(self):
        category_tree = (
            'self.__next_f.push([1, "{\\"categories\\":[{'
            '\\"name\\":\\"Tööriistad\\",\\"url_path\\":\\"tooriistad\\",'
            '\\"children\\":[{\\"name\\":\\"Akutrellid\\",\\"url_path\\":\\"tooriistad/akutrellid\\",\\"children\\":[]},'
            '{\\"name\\":\\"Saed\\",\\"url_path\\":\\"tooriistad/saed\\",\\"children\\":[]}]'
            '}]}"])'
        )
        products_document = (
            'self.__next_f.push([1, "{\\"nbPages\\":1,\\"nbHits\\":1,\\"hitsPerPage\\":40,'
            '\\"hits\\":[{\\"sku\\":\\"BH-1\\",\\"name\\":\\"Bauhaus drill\\",\\"url\\":\\"/bh-1\\",'
            '\\"bauhaus_price\\":{\\"final_price\\":{\\"value\\":\\"20.00\\",\\"currency\\":\\"EUR\\"}}}]}"])'
        )

        class FakeHttpClient:
            def __init__(self, log_callback=None):
                self.log_callback = log_callback
                self.category_calls = []

            async def __aenter__(self):
                if self.log_callback:
                    self.log_callback("BAUHAUS transport: curl_cffi impersonate=chrome")
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def log(self, message):
                if self.log_callback:
                    self.log_callback(message)

            async def get_text(self, url, **kwargs):
                if url == BAUHAUS_WEBSITE_URL:
                    return category_tree
                self.category_calls.append(url)
                return products_document

        logs = []
        with patch("parsers.services.bauhaus.BauhausHttpClient", FakeHttpClient):
            categories, products, complete = asyncio.run(
                BauhausClient(log_callback=logs.append, category_limit=1).fetch_products()
            )

        self.assertFalse(complete)
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].external_id, "BH-1")
        self.assertTrue(any("category limit applied: 1" in message for message in logs))
        self.assertEqual(sum(1 for category in categories if category.url.endswith(("akutrellid", "saed"))), 2)

    def test_homepage_429_uses_rsc_category_source(self):
        category_tree = (
            'self.__next_f.push([1, "{\\"categories\\":[{'
            '\\"name\\":\\"Tööriistad\\",\\"url_path\\":\\"tooriistad\\",'
            '\\"children\\":[{\\"name\\":\\"Akutrellid\\",\\"url_path\\":\\"tooriistad/akutrellid\\",\\"children\\":[]}]'
            '}]}"])'
        )
        products_document = (
            'self.__next_f.push([1, "{\\"nbPages\\":1,\\"nbHits\\":1,\\"hitsPerPage\\":40,'
            '\\"hits\\":[{\\"sku\\":\\"BH-9\\",\\"name\\":\\"Bauhaus drill\\",\\"url\\":\\"/bh-9\\"}]}"])'
        )

        class FakeHttpClient:
            def __init__(self, log_callback=None):
                self.log_callback = log_callback

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def log(self, message):
                if self.log_callback:
                    self.log_callback(message)

            async def get_text(self, url, **kwargs):
                endpoint = kwargs.get("endpoint_name", "")
                if endpoint == "homepage":
                    raise HttpRequestError("HTTP 429", status=429, retryable=True)
                if endpoint.startswith("RSC attempt"):
                    return category_tree
                return products_document

        logs = []
        with patch("parsers.services.bauhaus.BauhausHttpClient", FakeHttpClient):
            categories, products, complete = asyncio.run(
                BauhausClient(log_callback=logs.append, category_limit=1).fetch_products()
            )

        self.assertFalse(complete)
        self.assertEqual(products[0].external_id, "BH-9")
        self.assertTrue(any("homepage blocked by 429" in message for message in logs))

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
