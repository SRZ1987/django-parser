from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from parsers.models import ParserBatch, ParserQueueJob, ParserRun


STALE_RUN_MESSAGE = "Parser run marked failed because its heartbeat became stale."
STALE_JOB_MESSAGE = "Parser queue job marked failed because its heartbeat became stale."
STALE_BATCH_MESSAGE = "Parser batch marked failed because its heartbeat became stale."


@dataclass
class RecoveryResult:
    runs: int = 0
    jobs: int = 0
    batches: int = 0


def recover_stale_parser_state(now=None):
    now = now or timezone.now()
    return RecoveryResult(
        runs=recover_stale_runs(now),
        jobs=recover_stale_queue_jobs(now),
        batches=recover_stale_batches(now),
    )


def recover_stale_runs(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=settings.PARSER_STALE_RUN_MINUTES)
    recovered = 0
    for parser_run in ParserRun.objects.filter(status=ParserRun.STATUS_RUNNING).select_related("parser"):
        if not _is_stale(parser_run, cutoff):
            continue
        parser_run.status = ParserRun.STATUS_FAILED
        parser_run.stage = ParserRun.STAGE_COMPLETED
        parser_run.finished_at = now
        parser_run.error_message = STALE_RUN_MESSAGE
        parser_run.errors_count = max(parser_run.errors_count, 1)
        parser_run.save(update_fields=["status", "stage", "finished_at", "error_message", "errors_count"])
        recovered += 1
    return recovered


def recover_stale_queue_jobs(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=settings.PARSER_STALE_JOB_MINUTES)
    recovered = 0
    for job in ParserQueueJob.objects.filter(status=ParserQueueJob.STATUS_RUNNING):
        if not _is_stale(job, cutoff):
            continue
        job.status = ParserQueueJob.STATUS_FAILED
        job.finished_at = now
        job.error_message = STALE_JOB_MESSAGE
        job.save(update_fields=["status", "finished_at", "error_message"])
        recovered += 1
    return recovered


def recover_stale_batches(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=settings.PARSER_STALE_BATCH_MINUTES)
    recovered = 0
    for batch in ParserBatch.objects.filter(status=ParserBatch.STATUS_RUNNING):
        if not _is_stale(batch, cutoff):
            continue
        batch.status = ParserBatch.STATUS_FAILED
        batch.finished_at = now
        batch.current_parser = None
        batch.log = _append_log(batch.log, STALE_BATCH_MESSAGE)
        batch.save(update_fields=["status", "finished_at", "current_parser", "log"])
        recovered += 1
    return recovered


def _is_stale(obj, cutoff):
    timestamp = obj.heartbeat_at or obj.started_at
    return timestamp is not None and timestamp < cutoff


def _append_log(current_log, message):
    return f"{current_log}\n{message}" if current_log else message
