from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from parsers.models import ParserBatch, ParserBatchLock, ParserConfig, ParserRun
from parsers.standalone.public_commerce_parser import PUBLIC_COMMERCE_STORES
from parsers.standalone.sitemap_retailers_parser import SITEMAP_RETAILERS


class NightlyParsersCommandTests(TestCase):
    expected_order = [
        "espak",
        "depo",
        "bauhof",
        "ehituseabc",
        "fere",
        "bauhaus",
        "handymann",
        *(
            code
            for code, store in PUBLIC_COMMERCE_STORES.items()
            if store.enabled_by_default
        ),
        "oomipood",
        "lemona",
        *SITEMAP_RETAILERS,
        "motonet",
    ]

    def setUp(self):
        ParserBatchLock.objects.get_or_create(name="nightly_parser_batch")
        call_command("setup_parsers", verbosity=0, stdout=StringIO())

    def run_successful_command(self):
        calls = []

        def fake_run_excel_parser(parser_config, trigger):
            calls.append(parser_config.code)
            return ParserRun.objects.create(
                parser=parser_config,
                trigger=trigger,
                status=ParserRun.STATUS_SUCCESS,
                stage=ParserRun.STAGE_COMPLETED,
                started_at=timezone.now(),
                finished_at=timezone.now(),
            )

        output = StringIO()
        with patch("parsers.services.batch_runner.run_excel_parser", fake_run_excel_parser):
            call_command("run_nightly_parsers", stdout=output)
        return calls, output.getvalue()

    def test_successful_run_executes_all_enabled_parsers(self):
        calls, output = self.run_successful_command()

        batch = ParserBatch.objects.get()
        self.assertEqual(calls, self.expected_order)
        self.assertEqual(batch.status, ParserBatch.STATUS_SUCCESS)
        self.assertIsNone(batch.current_parser)
        self.assertIn(f"Enabled parsers: {len(self.expected_order)}", output)
        self.assertIn("Nightly parser batch completed successfully.", output)

    def test_run_order_is_respected(self):
        calls, _output = self.run_successful_command()

        configured_order = list(
            ParserConfig.objects.filter(code__in=self.expected_order).order_by("run_order", "name").values_list("code", flat=True)
        )
        self.assertEqual(configured_order, self.expected_order)
        self.assertEqual(calls, configured_order)

    def test_disabled_parser_is_not_started(self):
        ParserConfig.objects.filter(code="depo").update(is_enabled=False)

        calls, output = self.run_successful_command()

        self.assertNotIn("depo", calls)
        self.assertEqual(len(calls), len(self.expected_order) - 1)
        self.assertIn(f"Enabled parsers: {len(self.expected_order) - 1}", output)

    def test_real_active_batch_skips_second_start(self):
        running = ParserBatch.objects.create(
            status=ParserBatch.STATUS_RUNNING,
            started_at=timezone.now(),
            heartbeat_at=timezone.now(),
        )
        output = StringIO()

        call_command("run_nightly_parsers", stdout=output)

        running.refresh_from_db()
        self.assertEqual(running.status, ParserBatch.STATUS_RUNNING)
        self.assertEqual(ParserBatch.objects.filter(status=ParserBatch.STATUS_RUNNING).count(), 1)
        self.assertEqual(ParserRun.objects.count(), 0)
        self.assertIn("already running", output.getvalue())
        self.assertIn("skipped", output.getvalue())

    @override_settings(PARSER_STALE_BATCH_MINUTES=30)
    def test_stale_batch_is_recovered_then_new_batch_starts(self):
        old_time = timezone.now() - timedelta(minutes=31)
        stale_batch = ParserBatch.objects.create(
            status=ParserBatch.STATUS_RUNNING,
            started_at=old_time,
            heartbeat_at=old_time,
            log="previous run",
        )

        calls, output = self.run_successful_command()

        stale_batch.refresh_from_db()
        new_batch = ParserBatch.objects.exclude(pk=stale_batch.pk).get()
        self.assertEqual(stale_batch.status, ParserBatch.STATUS_FAILED)
        self.assertEqual(new_batch.status, ParserBatch.STATUS_SUCCESS)
        self.assertEqual(calls, self.expected_order)
        self.assertIn("batches=1", output)

    def test_parser_error_finishes_with_nonzero_command_error_without_running_state(self):
        calls = []

        def fake_run_excel_parser(parser_config, trigger):
            calls.append(parser_config.code)
            status = ParserRun.STATUS_FAILED if parser_config.code == "depo" else ParserRun.STATUS_SUCCESS
            return ParserRun.objects.create(
                parser=parser_config,
                trigger=trigger,
                status=status,
                stage=ParserRun.STAGE_COMPLETED,
                started_at=timezone.now(),
                finished_at=timezone.now(),
                error_message="boom" if status == ParserRun.STATUS_FAILED else "",
            )

        with patch("parsers.services.batch_runner.run_excel_parser", fake_run_excel_parser):
            with self.assertRaises(CommandError):
                call_command("run_nightly_parsers", stdout=StringIO())

        self.assertEqual(calls, self.expected_order)
        self.assertFalse(ParserBatch.objects.filter(status=ParserBatch.STATUS_RUNNING).exists())
        self.assertFalse(ParserRun.objects.filter(status=ParserRun.STATUS_RUNNING).exists())
        self.assertEqual(ParserBatch.objects.get().status, ParserBatch.STATUS_PARTIAL)

    def test_command_uses_existing_batch_runner_entrypoint(self):
        output = StringIO()
        batch = ParserBatch.objects.create(status=ParserBatch.STATUS_SUCCESS, started_at=timezone.now(), finished_at=timezone.now())

        with patch("parsers.management.commands.run_nightly_parsers.run_all_parsers", return_value=batch) as runner:
            call_command("run_nightly_parsers", stdout=output)

        runner.assert_called_once_with(trigger=ParserRun.TRIGGER_SCHEDULE)
        self.assertIn("ParserBatch ID", output.getvalue())
