from unittest.mock import patch

from django.test import TestCase

from catalog.models import Shop
from parsers.models import ParserConfig, ParserRun
from parsers.services.base import BaseStoreParser, ParserAlreadyRunningError, ParserResult
from parsers.services.runner import run_parser


class SuccessfulParser(BaseStoreParser):
    code = "depo"

    def run(self):
        return ParserResult(
            products_found=3,
            products_created=1,
            products_updated=2,
            prices_changed=1,
            errors_count=0,
        )


class FailingParser(BaseStoreParser):
    code = "depo"

    def run(self):
        raise RuntimeError("broken parser")


class ParserRunnerTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Depo", code="depo")
        self.parser_config = ParserConfig.objects.create(
            shop=self.shop,
            name="Depo parser",
            code="depo",
        )

    def test_creates_parser_run(self):
        with patch("parsers.services.runner.get_parser_class", return_value=SuccessfulParser):
            parser_run = run_parser("depo")

        self.assertEqual(parser_run.status, ParserRun.STATUS_SUCCESS)
        self.assertEqual(parser_run.products_found, 3)
        self.assertEqual(parser_run.products_created, 1)
        self.assertEqual(parser_run.products_updated, 2)
        self.assertEqual(parser_run.prices_changed, 1)
        self.assertFalse(ParserConfig.objects.get(pk=self.parser_config.pk).is_running)

    def test_resets_is_running_after_error(self):
        with patch("parsers.services.runner.get_parser_class", return_value=FailingParser):
            parser_run = run_parser("depo")

        self.assertEqual(parser_run.status, ParserRun.STATUS_FAILED)
        self.assertIn("broken parser", parser_run.error_message)
        self.assertFalse(ParserConfig.objects.get(pk=self.parser_config.pk).is_running)

    def test_prevents_second_run_when_parser_is_running(self):
        self.parser_config.is_running = True
        self.parser_config.save(update_fields=["is_running"])

        with self.assertRaises(ParserAlreadyRunningError):
            run_parser("depo")
