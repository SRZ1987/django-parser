from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from parsers.models import ParserBatch, ParserBatchLock, ParserConfig, ParserQueueJob, ParserRun
from parsers.services.batch_runner import process_next_queue_job
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
        *(code for code, store in PUBLIC_COMMERCE_STORES.items() if store.enabled_by_default),
        "oomipood",
        "lemona",
        *SITEMAP_RETAILERS,
        "motonet",
        "hammerjack",
        "stokker",
        "torujyri",
        "esvika",
        "arcade",
        "elektrikaup",
        "feb",
    ]

    def setUp(self):
        ParserBatchLock.objects.get_or_create(name="nightly_parser_batch")
        call_command("setup_parsers", verbosity=0, stdout=StringIO())

    def queue_nightly_job(self):
        output = StringIO()
        call_command("run_nightly_parsers", stdout=output)
        return ParserQueueJob.objects.get(), output.getvalue()

    def process_successful_job(self):
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

        with patch("parsers.services.batch_runner.run_excel_parser", fake_run_excel_parser):
            job = process_next_queue_job()
        return calls, job

    def test_command_queues_job_and_exits_without_running_batch_inline(self):
        with patch("parsers.services.batch_runner.run_all_parsers") as runner:
            job, output = self.queue_nightly_job()

        runner.assert_not_called()
        self.assertTrue(job.run_all)
        self.assertEqual(job.status, ParserQueueJob.STATUS_PENDING)
        self.assertEqual(job.trigger, ParserRun.TRIGGER_SCHEDULE)
        self.assertFalse(ParserBatch.objects.exists())
        self.assertIn(f"Enabled parsers: {len(self.expected_order)}", output)
        self.assertIn("queued for parser-worker", output)

    def test_worker_executes_all_enabled_parsers_in_order(self):
        self.queue_nightly_job()

        calls, job = self.process_successful_job()

        job.refresh_from_db()
        self.assertEqual(calls, self.expected_order)
        self.assertEqual(job.status, ParserQueueJob.STATUS_SUCCESS)
        self.assertEqual(job.batch.status, ParserBatch.STATUS_SUCCESS)
        self.assertIsNone(job.batch.current_parser)

    def test_disabled_parser_is_not_started(self):
        ParserConfig.objects.filter(code="depo").update(is_enabled=False)
        self.queue_nightly_job()

        calls, _job = self.process_successful_job()

        self.assertNotIn("depo", calls)
        self.assertEqual(len(calls), len(self.expected_order) - 1)

    def test_partial_batch_marks_queue_job_failed_but_runs_remaining_parsers(self):
        self.queue_nightly_job()
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
            job = process_next_queue_job()

        job.refresh_from_db()
        self.assertEqual(calls, self.expected_order)
        self.assertEqual(job.batch.status, ParserBatch.STATUS_PARTIAL)
        self.assertEqual(job.status, ParserQueueJob.STATUS_FAILED)
        self.assertIn("status=partial", job.error_message)

    def test_second_scheduler_does_not_duplicate_pending_job(self):
        first_job, _output = self.queue_nightly_job()
        second_output = StringIO()

        call_command("run_nightly_parsers", stdout=second_output)

        self.assertEqual(ParserQueueJob.objects.count(), 1)
        self.assertIn(f"ParserQueueJob ID: {first_job.pk}", second_output.getvalue())
        self.assertIn("already pending or running", second_output.getvalue())

    def test_running_all_parsers_job_is_not_duplicated(self):
        job = ParserQueueJob.objects.create(
            run_all=True,
            trigger=ParserRun.TRIGGER_SCHEDULE,
            status=ParserQueueJob.STATUS_RUNNING,
            started_at=timezone.now(),
            heartbeat_at=timezone.now(),
        )
        output = StringIO()

        call_command("run_nightly_parsers", stdout=output)

        self.assertEqual(ParserQueueJob.objects.count(), 1)
        self.assertIn(f"ParserQueueJob ID: {job.pk}", output.getvalue())

    def test_active_batch_skips_queueing(self):
        ParserBatch.objects.create(
            status=ParserBatch.STATUS_RUNNING,
            started_at=timezone.now(),
            heartbeat_at=timezone.now(),
        )
        output = StringIO()

        call_command("run_nightly_parsers", stdout=output)

        self.assertFalse(ParserQueueJob.objects.exists())
        self.assertIn("already running", output.getvalue())
        self.assertIn("skipped", output.getvalue())

    @override_settings(PARSER_STALE_BATCH_MINUTES=30)
    def test_stale_batch_is_recovered_before_job_is_queued(self):
        old_time = timezone.now() - timedelta(minutes=31)
        stale_batch = ParserBatch.objects.create(
            status=ParserBatch.STATUS_RUNNING,
            started_at=old_time,
            heartbeat_at=old_time,
            log="previous run",
        )

        _job, output = self.queue_nightly_job()

        stale_batch.refresh_from_db()
        self.assertEqual(stale_batch.status, ParserBatch.STATUS_FAILED)
        self.assertIn("batches=1", output)
