import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files import File
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook

from catalog.models import Product, ProductOffer, Shop
from parsers.adapters.espak import EspakAdapter
from parsers.models import ParserBatch, ParserConfig, ParserExport, ParserQueueJob, ParserRun
from parsers.services.batch_runner import ParserBatchAlreadyRunning, process_next_queue_job, run_all_parsers
from parsers.services.excel_importer import ExcelCatalogImporter
from parsers.services.excel_validation import ExcelCatalogValidator
from parsers.standalone import espak_parser


def create_xlsx(path, headers=None, rows=None):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(headers or espak_parser.COLUMNS)
    for row in rows or []:
        worksheet.append(row)
    workbook.save(path)


class ExcelValidationTests(TestCase):
    def test_empty_excel_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "empty.xlsx"
            create_xlsx(path)

            result = ExcelCatalogValidator().validate(path, column_map=EspakAdapter.column_map)

        self.assertFalse(result.is_valid)
        self.assertIn("empty", result.error_message.lower())

    def test_corrupted_excel_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "broken.xlsx"
            path.write_text("not an xlsx", encoding="utf-8")

            result = ExcelCatalogValidator().validate(path, column_map=EspakAdapter.column_map)

        self.assertFalse(result.is_valid)
        self.assertIn("cannot be opened", result.error_message)


class ExcelImportTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="ESPAK", code="espak")
        self.config = ParserConfig.objects.create(shop=self.shop, name="ESPAK parser", code="espak")
        self.run = ParserRun.objects.create(parser=self.config, trigger=ParserRun.TRIGGER_COMMAND)

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_successful_excel_import_creates_offer(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "espak.xlsx"
            create_xlsx(
                path,
                rows=[["Hammer", 10, 8, "", "4740000000001", "SKU-1", "https://img.test/a.jpg", "https://espak.test/a"]],
            )
            parser_export = self._create_export(path, rows_count=1)

            result = ExcelCatalogImporter().import_file(parser_export, column_map=EspakAdapter.column_map)

        offer = ProductOffer.objects.get(shop=self.shop, external_id="SKU-1")
        self.assertEqual(result.products_created, 1)
        self.assertEqual(offer.original_name, "Hammer")
        self.assertEqual(str(offer.price), "10.00")
        self.assertEqual(str(offer.sale_price), "8.00")
        self.assertEqual(offer.barcode, "4740000000001")

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_empty_barcode_does_not_overwrite_existing_barcode(self):
        product = Product.objects.create(name="Hammer", barcode="4740000000001")
        ProductOffer.objects.create(
            shop=self.shop,
            product=product,
            external_id="SKU-1",
            sku="SKU-1",
            barcode="4740000000001",
            original_name="Hammer",
            product_url="https://espak.test/a",
            image_url="https://img.test/a.jpg",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "espak.xlsx"
            create_xlsx(
                path,
                rows=[["Hammer updated", 11, "", "", "", "SKU-1", "", ""]],
            )
            parser_export = self._create_export(path, rows_count=1)

            ExcelCatalogImporter().import_file(parser_export, column_map=EspakAdapter.column_map)

        offer = ProductOffer.objects.get(shop=self.shop, external_id="SKU-1")
        self.assertEqual(offer.barcode, "4740000000001")
        self.assertEqual(offer.product_url, "https://espak.test/a")
        self.assertEqual(offer.image_url, "https://img.test/a.jpg")

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_failed_import_does_not_deactivate_existing_offer(self):
        product = Product.objects.create(name="Hammer")
        ProductOffer.objects.create(
            shop=self.shop,
            product=product,
            external_id="SKU-1",
            sku="SKU-1",
            original_name="Hammer",
            is_active=True,
            is_available=True,
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "espak.xlsx"
            create_xlsx(path, rows=[["", "", "", "", "", "", "", ""]])
            parser_export = self._create_export(path, rows_count=1)

            with self.assertRaises(ValueError):
                ExcelCatalogImporter().import_file(parser_export, column_map=EspakAdapter.column_map)

        offer = ProductOffer.objects.get(shop=self.shop, external_id="SKU-1")
        self.assertTrue(offer.is_active)
        self.assertTrue(offer.is_available)

    def _create_export(self, path, rows_count):
        parser_export = ParserExport(
            parser_run=self.run,
            shop=self.shop,
            original_filename=path.name,
            rows_count=rows_count,
            file_size=path.stat().st_size,
        )
        with open(path, "rb") as handle:
            parser_export.file.save(path.name, File(handle), save=True)
        return parser_export


class BatchRunnerTests(TestCase):
    def setUp(self):
        self.shop_a = Shop.objects.create(name="A", code="a")
        self.shop_b = Shop.objects.create(name="B", code="b")
        self.config_b = ParserConfig.objects.create(shop=self.shop_b, name="B parser", code="b", run_order=2)
        self.config_a = ParserConfig.objects.create(shop=self.shop_a, name="A parser", code="a", run_order=1)

    def test_parsers_run_in_run_order_and_continue_after_failure(self):
        calls = []

        def fake_run_excel_parser(parser_config, trigger):
            calls.append(parser_config.code)
            status = ParserRun.STATUS_FAILED if parser_config.code == "a" else ParserRun.STATUS_SUCCESS
            return ParserRun.objects.create(parser=parser_config, trigger=trigger, status=status)

        with patch("parsers.services.batch_runner.ADAPTERS", {"a": object, "b": object}):
            with patch("parsers.services.batch_runner.run_excel_parser", fake_run_excel_parser):
                batch = run_all_parsers()

        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(batch.status, ParserBatch.STATUS_PARTIAL)

    def test_running_batch_blocks_second_batch(self):
        ParserBatch.objects.create(status=ParserBatch.STATUS_RUNNING)

        with self.assertRaises(ParserBatchAlreadyRunning):
            run_all_parsers()

    def test_queue_job_is_processed_once(self):
        job = ParserQueueJob.objects.create(parser_config=self.config_a)

        def fake_run_excel_parser(parser_config, trigger):
            return ParserRun.objects.create(parser=parser_config, trigger=trigger, status=ParserRun.STATUS_SUCCESS)

        with patch("parsers.services.batch_runner.run_excel_parser", fake_run_excel_parser):
            processed = process_next_queue_job()
            second = process_next_queue_job()

        job.refresh_from_db()
        self.assertEqual(processed.pk, job.pk)
        self.assertIsNone(second)
        self.assertEqual(job.status, ParserQueueJob.STATUS_SUCCESS)
        self.assertEqual(job.attempts, 1)


class ParserExportAdminTests(TestCase):
    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_staff_with_permission_can_download_export(self):
        shop = Shop.objects.create(name="ESPAK", code="espak")
        config = ParserConfig.objects.create(shop=shop, name="ESPAK parser", code="espak")
        run = ParserRun.objects.create(parser=config, trigger=ParserRun.TRIGGER_COMMAND)
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "espak.xlsx"
            create_xlsx(path, rows=[["Hammer", 10, "", "", "", "SKU-1", "", ""]])
            parser_export = ParserExport(parser_run=run, shop=shop, original_filename=path.name, rows_count=1)
            with open(path, "rb") as handle:
                parser_export.file.save(path.name, File(handle), save=True)

        user = get_user_model().objects.create_superuser("admin", "admin@example.com", "password")
        client = Client()
        client.force_login(user)
        response = client.get(reverse("admin:parsers_parserexport_download", args=[parser_export.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_anonymous_user_cannot_download_export(self):
        shop = Shop.objects.create(name="ESPAK", code="espak")
        config = ParserConfig.objects.create(shop=shop, name="ESPAK parser", code="espak")
        run = ParserRun.objects.create(parser=config, trigger=ParserRun.TRIGGER_COMMAND)
        parser_export = ParserExport.objects.create(
            parser_run=run,
            shop=shop,
            original_filename="espak.xlsx",
            rows_count=1,
        )

        response = Client().get(reverse("admin:parsers_parserexport_download", args=[parser_export.pk]))

        self.assertEqual(response.status_code, 302)
