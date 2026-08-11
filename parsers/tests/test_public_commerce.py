import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.files import File
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from openpyxl import load_workbook

from catalog.models import ProductOffer, Shop
from parsers.adapters.public_commerce import (
    DecoraAdapter,
    EmartAdapter,
    HordenAdapter,
    PUBLIC_COMMERCE_ADAPTERS,
)
from parsers.models import ParserConfig, ParserExport, ParserRun
from parsers.services.excel_importer import ExcelCatalogImporter
from parsers.services.excel_validation import ExcelCatalogValidator
from parsers.standalone import public_commerce_parser


def woo_product(
    product_id=101,
    *,
    sku="SHOP-101",
    name="Construction product",
    price="1299",
    regular_price="1599",
    sale_price="1299",
    in_stock=True,
):
    return {
        "id": product_id,
        "name": name,
        "sku": sku,
        "permalink": f"https://www.emart.ee/toode/{product_id}/",
        "is_in_stock": in_stock,
        "prices": {
            "price": price,
            "regular_price": regular_price,
            "sale_price": sale_price,
            "currency_minor_unit": 2,
        },
        "images": [{"src": f"https://img.test/{product_id}.jpg"}],
        "categories": [{"id": 7, "name": "Fasteners"}],
        "description": "Product description",
        "brands": [{"name": "SUKI"}],
        "attributes": [{"name": "Model", "terms": [{"name": "NAEL-100"}]}],
        "extensions": {"catalog": {"ean": "4740000000001"}},
    }


def shopify_product(product_id=201, variant_id=301, *, available=True):
    return {
        "id": product_id,
        "title": "Roofing sheet",
        "handle": "roofing-sheet",
        "images": [{"src": "https://img.test/roof.jpg"}],
        "product_type": "Roofing",
        "body_html": "<p>Roof product</p>",
        "variants": [
            {
                "id": variant_id,
                "title": "Black",
                "sku": "HOR-301",
                "barcode": "4740000000002",
                "price": "12.99",
                "compare_at_price": "15.99",
                "available": available,
            }
        ],
    }


def klevu_product(product_id=401, *, in_stock="yes"):
    return {
        "id": str(product_id),
        "sku": "DEC-401",
        "name": "Construction board",
        "price": "19.99",
        "salePrice": "14.99",
        "inStock": in_stock,
        "url": "https://www.decora.ee/construction-board",
        "imageUrl": "https://www.decora.ee/media/board.jpg",
        "category": "Boards",
        "klevu_category": "KLEVU_PRODUCT;;Building materials;Boards",
        "shortDesc": "Durable construction board",
        "ean": "4740000000003",
    }


def klevu_payload(records, total=None):
    return {
        "queryResults": [
            {
                "meta": {"totalResultsFound": len(records) if total is None else total},
                "records": records,
            }
        ]
    }


class PublicCommerceNormalizationTests(SimpleTestCase):
    def test_woocommerce_uses_product_id_for_external_id_and_preserves_sku(self):
        row = public_commerce_parser.normalize_woocommerce_product(woo_product())

        self.assertEqual(row[0], "Construction product")
        self.assertEqual(row[1], 15.99)
        self.assertEqual(row[2], 12.99)
        self.assertEqual(row[4], "4740000000001")
        self.assertEqual(row[5], "wc-101")
        self.assertEqual(row[8], "SHOP-101")
        self.assertEqual(row[9], "Fasteners")
        self.assertEqual(row[10], "wc-category-7")
        self.assertEqual(row[11], "Product description")
        self.assertEqual(row[12], "SUKI")
        self.assertEqual(row[13], "NAEL-100")

    def test_woocommerce_without_sku_keeps_stable_external_id(self):
        row = public_commerce_parser.normalize_woocommerce_product(woo_product(sku=""))

        self.assertEqual(row[5], "wc-101")
        self.assertEqual(row[8], "")

    def test_woocommerce_out_of_stock_product_is_skipped(self):
        self.assertIsNone(
            public_commerce_parser.normalize_woocommerce_product(woo_product(in_stock=False))
        )

    def test_woocommerce_product_without_positive_price_is_skipped(self):
        self.assertIsNone(
            public_commerce_parser.normalize_woocommerce_product(
                woo_product(price="0", regular_price="0", sale_price="0")
            )
        )

    def test_shopify_variant_has_stable_id_prices_and_barcode(self):
        rows = public_commerce_parser.normalize_shopify_product(
            shopify_product(),
            "https://horden.ee/",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Roofing sheet - Black")
        self.assertEqual(rows[0][1], 15.99)
        self.assertEqual(rows[0][2], 12.99)
        self.assertEqual(rows[0][4], "4740000000002")
        self.assertEqual(rows[0][5], "shopify-301")
        self.assertEqual(rows[0][8], "HOR-301")
        self.assertEqual(rows[0][9], "Roofing")
        self.assertEqual(rows[0][11], "Roof product")

    def test_klevu_product_uses_record_id_and_preserves_sku(self):
        row = public_commerce_parser.normalize_klevu_product(klevu_product())

        self.assertEqual(row[0], "Construction board")
        self.assertEqual(row[1], 19.99)
        self.assertEqual(row[2], 14.99)
        self.assertEqual(row[4], "4740000000003")
        self.assertEqual(row[5], "klevu-401")
        self.assertEqual(row[8], "DEC-401")
        self.assertEqual(row[9], "Boards")
        self.assertTrue(row[10].startswith("klevu-category-"))

    def test_klevu_out_of_stock_product_is_skipped(self):
        self.assertIsNone(
            public_commerce_parser.normalize_klevu_product(klevu_product(in_stock="no"))
        )

    def test_klevu_placeholder_image_path_is_normalized(self):
        product = klevu_product()
        product["imageUrl"] = (
            "https://www.decora.ee/needtochange/media/klevu_images/200X200/product.jpg"
        )

        row = public_commerce_parser.normalize_klevu_product(product)

        self.assertEqual(
            row[6],
            "https://www.decora.ee/media/klevu_images/200X200/product.jpg",
        )


class PublicCommerceDownloadTests(SimpleTestCase):
    def test_woocommerce_fetches_every_page(self):
        store = public_commerce_parser.PUBLIC_COMMERCE_STORES["emart"]
        calls = []

        async def fake_request_json(_session, _store, *, params, label, log_callback=None):
            calls.append(params["page"])
            return [woo_product(product_id=params["page"])], {
                "X-WP-Total": "2",
                "X-WP-TotalPages": "2",
            }

        with patch("parsers.standalone.public_commerce_parser.request_json", fake_request_json):
            products = asyncio.run(
                public_commerce_parser.fetch_woocommerce_catalog(None, store)
            )

        self.assertEqual({product["id"] for product in products}, {1, 2})
        self.assertEqual(sorted(calls), [1, 2])

    def test_incomplete_woocommerce_catalog_fails(self):
        store = public_commerce_parser.PUBLIC_COMMERCE_STORES["emart"]

        async def fake_request_json(_session, _store, *, params, label, log_callback=None):
            return [woo_product(product_id=params["page"])], {
                "X-WP-Total": "3",
                "X-WP-TotalPages": "2",
            }

        with patch("parsers.standalone.public_commerce_parser.request_json", fake_request_json):
            with self.assertRaisesRegex(RuntimeError, "catalog is incomplete"):
                asyncio.run(public_commerce_parser.fetch_woocommerce_catalog(None, store))

    def test_shopify_stops_after_short_final_page(self):
        store = public_commerce_parser.PUBLIC_COMMERCE_STORES["horden"]
        calls = []

        async def fake_request_json(_session, _store, *, params, label, log_callback=None):
            calls.append(params["page"])
            return {"products": [shopify_product()]}, {}

        with patch("parsers.standalone.public_commerce_parser.request_json", fake_request_json):
            products = asyncio.run(public_commerce_parser.fetch_shopify_catalog(None, store))

        self.assertEqual(len(products), 1)
        self.assertEqual(calls, [1])

    def test_klevu_fetches_all_offsets_and_checks_completeness(self):
        store = public_commerce_parser.PUBLIC_COMMERCE_STORES["decora"]
        calls = []

        async def fake_request_json(
            _session,
            _store,
            *,
            params=None,
            json_payload=None,
            label,
            log_callback=None,
        ):
            settings = json_payload["recordQueries"][0]["settings"]
            offset = settings["offset"]
            limit = settings["limit"]
            calls.append(offset)
            return klevu_payload(
                [klevu_product(product_id=offset + index) for index in range(limit)],
                total=250,
            ), {}

        with patch("parsers.standalone.public_commerce_parser.request_json", fake_request_json):
            products = asyncio.run(public_commerce_parser.fetch_klevu_catalog(None, store))

        self.assertEqual(len(products), 250)
        self.assertEqual(sorted(calls), [0, 100, 200])


class PublicCommerceAdapterTests(SimpleTestCase):
    def test_all_store_definitions_have_matching_adapters(self):
        self.assertEqual(
            {adapter.code for adapter in PUBLIC_COMMERCE_ADAPTERS},
            set(public_commerce_parser.PUBLIC_COMMERCE_STORES),
        )

    def test_woocommerce_adapter_creates_excel_and_counts_rows(self):
        async def fake_main(store_code, output_path, log_callback=None):
            self.assertEqual(store_code, "emart")
            public_commerce_parser.save_excel(
                [public_commerce_parser.normalize_woocommerce_product(woo_product())],
                Path(output_path),
            )
            log_callback("Emart progress")

        logs = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "emart.xlsx"
            with patch("parsers.adapters.public_commerce.public_commerce_parser.main", fake_main):
                result = asyncio.run(EmartAdapter().run(output_path, log_callback=logs.append))

        self.assertTrue(result.success)
        self.assertEqual(result.products_count, 1)
        self.assertEqual(logs, ["Emart progress"])

    def test_shopify_adapter_uses_shared_excel_contract(self):
        self.assertEqual(HordenAdapter.column_map[public_commerce_parser.COLUMNS[8]], "sku")

    def test_decora_adapter_uses_shared_excel_contract(self):
        self.assertEqual(DecoraAdapter.code, "decora")
        self.assertEqual(DecoraAdapter.column_map[public_commerce_parser.COLUMNS[8]], "sku")

    def test_decora_adapter_creates_excel_and_counts_rows(self):
        async def fake_main(store_code, output_path, log_callback=None):
            self.assertEqual(store_code, "decora")
            public_commerce_parser.save_excel(
                [public_commerce_parser.normalize_klevu_product(klevu_product())],
                Path(output_path),
            )
            log_callback("Decora progress")

        logs = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "decora.xlsx"
            with patch("parsers.adapters.public_commerce.public_commerce_parser.main", fake_main):
                result = asyncio.run(DecoraAdapter().run(output_path, log_callback=logs.append))

        self.assertTrue(result.success)
        self.assertEqual(result.products_count, 1)
        self.assertEqual(logs, ["Decora progress"])


class PublicCommerceExcelPipelineTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(
            name="Emart",
            code="emart",
            website_url="https://www.emart.ee/",
        )
        self.config = ParserConfig.objects.create(
            shop=self.shop,
            name="Emart parser",
            code="emart",
            run_order=8,
        )
        self.parser_run = ParserRun.objects.create(
            parser=self.config,
            status=ParserRun.STATUS_RUNNING,
            started_at=timezone.now(),
        )

    def test_excel_validates_and_imports_external_id_and_sku_separately(self):
        row = public_commerce_parser.normalize_woocommerce_product(woo_product())
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "emart.xlsx"
            public_commerce_parser.save_excel([row], path)
            validation = ExcelCatalogValidator().validate(
                path,
                column_map=EmartAdapter.column_map,
                worksheet_name=EmartAdapter.worksheet_name,
            )
            self.assertTrue(validation.is_valid, validation.error_message)

            parser_export = ParserExport(
                parser_run=self.parser_run,
                shop=self.shop,
                original_filename=path.name,
                rows_count=validation.rows_count,
                file_size=path.stat().st_size,
            )
            with path.open("rb") as handle:
                parser_export.file.save(path.name, File(handle), save=True)

            result = ExcelCatalogImporter().import_file(
                parser_export,
                column_map=EmartAdapter.column_map,
                worksheet_name=EmartAdapter.worksheet_name,
            )

        offer = ProductOffer.objects.get(shop=self.shop, external_id="wc-101")
        self.assertEqual(result.products_created, 1)
        self.assertEqual(offer.sku, "SHOP-101")
        self.assertEqual(offer.category.name, "Fasteners")
        self.assertEqual(offer.description, "Product description")
        self.assertEqual(offer.product.brand, "SUKI")
        self.assertEqual(offer.product.model, "NAEL-100")
        self.assertEqual(str(offer.price), "15.99")
        self.assertEqual(str(offer.sale_price), "12.99")

    def test_reimport_updates_existing_offer_without_duplicate(self):
        first_row = public_commerce_parser.normalize_woocommerce_product(woo_product())
        updated_row = public_commerce_parser.normalize_woocommerce_product(
            woo_product(price="1099", regular_price="1099", sale_price="")
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            for index, row in enumerate((first_row, updated_row), start=1):
                path = Path(tmp_dir) / f"emart-{index}.xlsx"
                public_commerce_parser.save_excel([row], path)
                parser_run = self.parser_run
                if index == 2:
                    parser_run = ParserRun.objects.create(
                        parser=self.config,
                        status=ParserRun.STATUS_RUNNING,
                        started_at=timezone.now(),
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
                ExcelCatalogImporter().import_file(
                    parser_export,
                    column_map=EmartAdapter.column_map,
                    worksheet_name=EmartAdapter.worksheet_name,
                )
                if index == 1:
                    parser_run.status = ParserRun.STATUS_SUCCESS
                    parser_run.finished_at = timezone.now()
                    parser_run.save(update_fields=["status", "finished_at"])

        self.assertEqual(ProductOffer.objects.filter(shop=self.shop).count(), 1)
        offer = ProductOffer.objects.get(shop=self.shop, external_id="wc-101")
        self.assertEqual(str(offer.price), "10.99")
        self.assertIsNone(offer.sale_price)

    def test_decora_excel_validates_and_imports_product(self):
        shop = Shop.objects.create(
            name="Decora",
            code="decora",
            website_url="https://www.decora.ee/",
        )
        config = ParserConfig.objects.create(
            shop=shop,
            name="Decora parser",
            code="decora",
            run_order=27,
        )
        parser_run = ParserRun.objects.create(
            parser=config,
            status=ParserRun.STATUS_RUNNING,
            started_at=timezone.now(),
        )
        row = public_commerce_parser.normalize_klevu_product(klevu_product())

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "decora.xlsx"
            public_commerce_parser.save_excel([row], path)
            validation = ExcelCatalogValidator().validate(
                path,
                column_map=DecoraAdapter.column_map,
                worksheet_name=DecoraAdapter.worksheet_name,
            )
            self.assertTrue(validation.is_valid, validation.error_message)

            parser_export = ParserExport(
                parser_run=parser_run,
                shop=shop,
                original_filename=path.name,
                rows_count=validation.rows_count,
                file_size=path.stat().st_size,
            )
            with path.open("rb") as handle:
                parser_export.file.save(path.name, File(handle), save=True)

            result = ExcelCatalogImporter().import_file(
                parser_export,
                column_map=DecoraAdapter.column_map,
                worksheet_name=DecoraAdapter.worksheet_name,
            )

        offer = ProductOffer.objects.get(shop=shop, external_id="klevu-401")
        self.assertEqual(result.products_created, 1)
        self.assertEqual(offer.sku, "DEC-401")
        self.assertEqual(offer.category.name, "Boards")
        self.assertEqual(str(offer.price), "19.99")
        self.assertEqual(str(offer.sale_price), "14.99")
