import asyncio
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from catalog.models import Shop
from parsers.models import ParserConfig, ParserRun
from parsers.services.base import BaseStoreParser, ParserAlreadyRunningError, ParserResult
from parsers.services.runner import STALE_RUNNING_LOCK_AFTER, run_parser


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


class KeyboardInterruptParser(BaseStoreParser):
    code = "depo"

    def run(self):
        raise KeyboardInterrupt


class CancelledParser(BaseStoreParser):
    code = "depo"

    def run(self):
        raise asyncio.CancelledError


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
        ParserRun.objects.create(
            parser=self.parser_config,
            status=ParserRun.STATUS_RUNNING,
            trigger=ParserRun.TRIGGER_COMMAND,
            started_at=timezone.now(),
        )

        with self.assertRaises(ParserAlreadyRunningError):
            run_parser("depo")

    def test_keyboard_interrupt_resets_is_running_and_marks_failed(self):
        with patch("parsers.services.runner.get_parser_class", return_value=KeyboardInterruptParser):
            with self.assertRaises(KeyboardInterrupt):
                run_parser("depo")

        parser_config = ParserConfig.objects.get(pk=self.parser_config.pk)
        parser_run = ParserRun.objects.get(parser=parser_config)
        self.assertFalse(parser_config.is_running)
        self.assertEqual(parser_config.last_status, ParserConfig.STATUS_FAILED)
        self.assertIsNotNone(parser_config.last_finished_at)
        self.assertIn("Parser interrupted manually", parser_config.last_error)
        self.assertEqual(parser_run.status, ParserRun.STATUS_FAILED)
        self.assertEqual(parser_run.error_message, "Parser interrupted manually")
        self.assertIsNotNone(parser_run.finished_at)

    def test_cancelled_error_resets_is_running_and_marks_failed(self):
        with patch("parsers.services.runner.get_parser_class", return_value=CancelledParser):
            with self.assertRaises(asyncio.CancelledError):
                run_parser("depo")

        parser_config = ParserConfig.objects.get(pk=self.parser_config.pk)
        parser_run = ParserRun.objects.get(parser=parser_config)
        self.assertFalse(parser_config.is_running)
        self.assertEqual(parser_config.last_status, ParserConfig.STATUS_FAILED)
        self.assertEqual(parser_run.status, ParserRun.STATUS_FAILED)
        self.assertEqual(parser_run.error_message, "Parser cancelled")

    def test_stale_is_running_without_running_run_is_cleared(self):
        self.parser_config.is_running = True
        self.parser_config.last_status = ParserConfig.STATUS_RUNNING
        self.parser_config.save(update_fields=["is_running", "last_status"])

        with patch("parsers.services.runner.get_parser_class", return_value=SuccessfulParser):
            parser_run = run_parser("depo")

        parser_config = ParserConfig.objects.get(pk=self.parser_config.pk)
        self.assertEqual(parser_run.status, ParserRun.STATUS_SUCCESS)
        self.assertFalse(parser_config.is_running)

    def test_old_running_run_is_considered_stale_and_cleared(self):
        old_started_at = timezone.now() - STALE_RUNNING_LOCK_AFTER - timedelta(minutes=1)
        self.parser_config.is_running = True
        self.parser_config.last_status = ParserConfig.STATUS_RUNNING
        self.parser_config.save(update_fields=["is_running", "last_status"])
        old_run = ParserRun.objects.create(
            parser=self.parser_config,
            status=ParserRun.STATUS_RUNNING,
            trigger=ParserRun.TRIGGER_COMMAND,
            started_at=old_started_at,
        )

        with patch("parsers.services.runner.get_parser_class", return_value=SuccessfulParser):
            parser_run = run_parser("depo")

        old_run.refresh_from_db()
        self.assertEqual(old_run.status, ParserRun.STATUS_FAILED)
        self.assertIn("Stale parser running lock cleared", old_run.error_message)
        self.assertEqual(parser_run.status, ParserRun.STATUS_SUCCESS)
