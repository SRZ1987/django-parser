import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from django.core.files import File
from django.test import SimpleTestCase, TestCase, override_settings

from catalog.models import ProductOffer, Shop
from parsers.adapters.feb import FebAdapter
from parsers.adapters.registry import ADAPTERS, get_adapter_class
from parsers.models import ParserConfig, ParserExport, ParserRun
from parsers.services.excel_importer import ExcelCatalogImporter
from parsers.services.excel_validation import ExcelCatalogValidator
from parsers.standalone import feb_parser


def feb_html(
    product_id="197096",
    *,
    product_url="https://www.feb.ee/et/product-197096",
    availability="InStock",
):
    return f"""
    <!doctype html>
    <html>
      <head>
        <script type="application/ld+json">
          {{
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
              {{"@type": "ListItem", "position": 1, "item": {{"name": "Avaleht", "@id": "https://www.feb.ee/et/"}}}},
              {{"@type": "ListItem", "position": 2, "item": {{"name": "Vannituba", "@id": "https://www.feb.ee/et/vannituba"}}}},
              {{"@type": "ListItem", "position": 3, "item": {{"name": "Test product", "@id": "{product_url}"}}}}
            ]
          }}
        </script>
        <script type="application/ld+json">
          {{
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "Bidee käsidušikomplekt Oras Optima",
            "productID": "{product_id}",
            "sku": "{product_id}",
            "brand": {{"@type": "Brand", "name": "Oras"}},
            "model": "Optima",
            "image": "https://img.test/{product_id}.jpg",
            "description": "Termostaadiga käsidušikomplekt",
            "gtin13": "6414150100005",
            "offers": {{
              "@type": "Offer",
              "url": "{product_url}",
              "price": "303.00",
              "priceCurrency": "EUR",
              "availability": "https://schema.org/{availability}"
            }}
          }}
        </script>
      </head>
      <body>
        <span class="old-price">
          <span data-price-type="oldPrice" data-price-amount="629.00">629.00</span>
        </span>
      </body>
    </html>
    """


def sitemap_xml(*urls):
    entries = "".join(
        f"""
        <url>
          <loc>{url}</loc>
          <image:image><image:loc>https://img.test/{index}.jpg</image:loc></image:image>
        </url>
        """
        for index, url in enumerate(urls, start=1)
    )
    return f"""
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
      {entries}
    </urlset>
    """


class NoopPacer:
    async def wait(self):
        return None


class FakeResponse:
    def __init__(self, url, status, text="", headers=None):
        self.url = url
        self.status = status
        self._text = text
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def text(self):
        return self._text


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url):
        self.calls += 1
        response = self.responses.pop(0)
        response.url = url
        return response


class FebProductParsingTests(SimpleTestCase):
    def test_product_uses_public_jsonld_sale_price_and_identifiers(self):
        row = feb_parser.parse_product_page(
            feb_html(),
            "https://www.feb.ee/et/soodusmuuk/example-197096",
        )

        self.assertEqual(row[0], "Bidee käsidušikomplekt Oras Optima")
        self.assertEqual(row[1], 629.0)
        self.assertEqual(row[2], 303.0)
        self.assertEqual(row[5], "feb-197096")
        self.assertEqual(row[4], "6414150100005")
        self.assertEqual(row[7], "https://www.feb.ee/et/product-197096")
        self.assertEqual(row[8], "197096")
        self.assertEqual(row[9], "Vannituba")
        self.assertEqual(row[12], "Oras")
        self.assertEqual(row[13], "Optima")

    def test_out_of_stock_product_is_not_exported(self):
        row = feb_parser.parse_product_page(
            feb_html(availability="OutOfStock"),
            "https://www.feb.ee/et/product-197096",
        )

        self.assertIsNone(row)

    def test_missing_availability_fails_instead_of_assuming_product_is_available(self):
        html = feb_html().replace(
            '"availability": "https://schema.org/InStock"',
            '"availability": ""',
        )

        with self.assertRaisesRegex(feb_parser.ProductMarkupMissing, "availability is missing"):
            feb_parser.parse_product_page(html, "https://www.feb.ee/et/product-197096")


class FebNetworkSafetyTests(SimpleTestCase):
    def test_anomalously_small_sitemap_is_rejected(self):
        with self.assertRaisesRegex(feb_parser.CatalogIncomplete, "anomalously small"):
            feb_parser.validate_catalog_snapshot(
                sitemap_products=feb_parser.MIN_SITEMAP_PRODUCTS - 1,
                exported_products=1,
                missing_products=0,
            )

    def test_retry_after_is_honored_for_429(self):
        url = "https://www.feb.ee/et/product-197096"
        session = FakeSession(
            [
                FakeResponse(url, 429, "slow down", {"Retry-After": "3"}),
                FakeResponse(url, 200, feb_html()),
            ]
        )

        with patch.object(feb_parser.asyncio, "sleep", AsyncMock()) as sleep_mock:
            result = asyncio.run(
                feb_parser.request_text(
                    session,
                    url,
                    label="product",
                    pacer=NoopPacer(),
                )
            )

        self.assertIn("Bidee", result)
        self.assertEqual(session.calls, 2)
        sleep_mock.assert_awaited_once_with(3.0)

    def test_429_retries_are_limited(self):
        url = "https://www.feb.ee/et/product-197096"
        session = FakeSession(
            [FakeResponse(url, 429, "slow down", {"Retry-After": "0"})]
            * feb_parser.MAX_RETRIES
        )

        with patch.object(feb_parser.asyncio, "sleep", AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                asyncio.run(
                    feb_parser.request_text(
                        session,
                        url,
                        label="product",
                        pacer=NoopPacer(),
                    )
                )

        self.assertEqual(session.calls, feb_parser.MAX_RETRIES)

    def test_request_pacer_limits_request_start_rate(self):
        clock_values = iter([10.0, 10.0])
        pacer = feb_parser.RequestPacer(4, clock=lambda: next(clock_values))

        async def wait_twice():
            await pacer.wait()
            await pacer.wait()

        with patch.object(feb_parser.asyncio, "sleep", AsyncMock()) as sleep_mock:
            asyncio.run(wait_twice())

        sleep_mock.assert_awaited_once_with(0.25)

    def test_worker_pool_never_exceeds_configured_concurrency(self):
        active = 0
        maximum_active = 0
        urls = [f"https://www.feb.ee/et/product-{index}" for index in range(12)]

        async def fake_request_text(_session, url, **_kwargs):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.001)
            product_id = url.rsplit("-", 1)[-1]
            active -= 1
            return feb_html(product_id, product_url=url)

        with (
            patch.object(feb_parser, "request_text", fake_request_text),
            patch.object(feb_parser, "MIN_SITEMAP_PRODUCTS", 1),
        ):
            rows, stats = asyncio.run(
                feb_parser.fetch_rows(
                    None,
                    [(url, "") for url in urls],
                    concurrency=3,
                    pacer=NoopPacer(),
                )
            )

        self.assertEqual(len(rows), len(urls))
        self.assertEqual(stats.processed, len(urls))
        self.assertLessEqual(maximum_active, 3)

    def test_worker_error_finishes_queue_and_fails_complete_catalog(self):
        urls = ["https://www.feb.ee/et/good-1", "https://www.feb.ee/et/bad-2"]

        async def fake_request_text(_session, url, **_kwargs):
            if "bad" in url:
                raise RuntimeError("network exhausted")
            return feb_html("1", product_url=url)

        async def run_fetch():
            return await asyncio.wait_for(
                feb_parser.fetch_rows(
                    None,
                    [(url, "") for url in urls],
                    concurrency=2,
                    pacer=NoopPacer(),
                ),
                timeout=1,
            )

        with (
            patch.object(feb_parser, "request_text", fake_request_text),
            patch.object(feb_parser, "MIN_SITEMAP_PRODUCTS", 1),
        ):
            with self.assertRaisesRegex(feb_parser.CatalogIncomplete, "failed_pages=1"):
                asyncio.run(run_fetch())

    def test_duplicate_external_id_for_different_urls_fails_catalog(self):
        urls = ["https://www.feb.ee/et/product-a", "https://www.feb.ee/et/product-b"]

        async def fake_request_text(_session, url, **_kwargs):
            return feb_html("same-id", product_url=url)

        with (
            patch.object(feb_parser, "request_text", fake_request_text),
            patch.object(feb_parser, "MIN_SITEMAP_PRODUCTS", 1),
        ):
            with self.assertRaisesRegex(feb_parser.CatalogIncomplete, "duplicate external_id"):
                asyncio.run(
                    feb_parser.fetch_rows(
                        None,
                        [(url, "") for url in urls],
                        concurrency=2,
                        pacer=NoopPacer(),
                    )
                )

    def test_incomplete_catalog_does_not_create_excel(self):
        product_url = "https://www.feb.ee/et/product-1"

        async def fake_request_text(_session, _url, *, label, **_kwargs):
            if label == "product sitemap":
                return sitemap_xml(product_url)
            return "<html><body>Missing product data</body></html>"

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "feb.xlsx"
            with (
                patch.object(feb_parser, "request_text", fake_request_text),
                patch.object(feb_parser, "MIN_SITEMAP_PRODUCTS", 1),
                patch.object(feb_parser.asyncio, "sleep", AsyncMock()),
            ):
                with self.assertRaises(feb_parser.CatalogIncomplete):
                    asyncio.run(feb_parser.main(output_path))

            self.assertFalse(output_path.exists())


class FebAdapterTests(SimpleTestCase):
    def test_adapter_creates_valid_excel_and_counts_rows(self):
        row = feb_parser.parse_product_page(
            feb_html(),
            "https://www.feb.ee/et/product-197096",
        )

        async def fake_main(output_path, log_callback=None):
            feb_parser.save_excel([row], Path(output_path))
            log_callback("FEB progress")

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "feb.xlsx"
            logs = []
            with patch("parsers.adapters.feb.feb_parser.main", fake_main):
                result = asyncio.run(FebAdapter().run(output_path, log_callback=logs.append))
            validation = ExcelCatalogValidator().validate(
                output_path,
                column_map=FebAdapter.column_map,
                worksheet_name=FebAdapter.worksheet_name,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.products_count, 1)
        self.assertEqual(logs, ["FEB progress"])
        self.assertTrue(validation.is_valid, validation.error_message)
        self.assertEqual(validation.rows_count, 1)

    def test_registry_contains_feb_adapter(self):
        self.assertIs(ADAPTERS["feb"], FebAdapter)
        self.assertIs(get_adapter_class("feb"), FebAdapter)


class FebImporterIntegrationTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.get(code="feb")
        self.config = ParserConfig.objects.get(code="feb")

    def _create_export(self, path):
        parser_run = ParserRun.objects.create(
            parser=self.config,
            trigger=ParserRun.TRIGGER_COMMAND,
        )
        parser_export = ParserExport(
            parser_run=parser_run,
            shop=self.shop,
            original_filename=path.name,
            rows_count=1,
            file_size=path.stat().st_size,
        )
        with path.open("rb") as handle:
            parser_export.file.save(path.name, File(handle), save=True)
        return parser_export

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_feb_excel_imports_and_reimport_updates_without_duplicates(self):
        row = feb_parser.parse_product_page(
            feb_html(),
            "https://www.feb.ee/et/product-197096",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "feb.xlsx"
            feb_parser.save_excel([row], path)
            first_result = ExcelCatalogImporter().import_file(
                self._create_export(path),
                column_map=FebAdapter.column_map,
                worksheet_name=FebAdapter.worksheet_name,
            )

            row[2] = 299.0
            feb_parser.save_excel([row], path)
            second_result = ExcelCatalogImporter().import_file(
                self._create_export(path),
                column_map=FebAdapter.column_map,
                worksheet_name=FebAdapter.worksheet_name,
            )

        offer = ProductOffer.objects.get(shop=self.shop, external_id="feb-197096")
        self.assertEqual(first_result.products_created, 1)
        self.assertEqual(second_result.products_updated, 1)
        self.assertEqual(ProductOffer.objects.filter(shop=self.shop).count(), 1)
        self.assertEqual(str(offer.sale_price), "299.00")
        self.assertEqual(offer.barcode, "6414150100005")
        self.assertEqual(offer.product.brand, "Oras")
        self.assertEqual(offer.product.model, "Optima")
