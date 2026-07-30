from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db.models import F
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
        updated = _stale_filter(ParserRun.objects.filter(pk=parser_run.pk, status=ParserRun.STATUS_RUNNING), parser_run, cutoff).update(
            status=ParserRun.STATUS_FAILED,
            stage=ParserRun.STAGE_COMPLETED,
            finished_at=now,
            error_message=STALE_RUN_MESSAGE,
            errors_count=F("errors_count") + 1,
        )
        recovered += updated
    return recovered


def recover_stale_queue_jobs(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=settings.PARSER_STALE_JOB_MINUTES)
    recovered = 0
    for job in ParserQueueJob.objects.filter(status=ParserQueueJob.STATUS_RUNNING):
        if not _is_stale(job, cutoff):
            continue
        updated = _stale_filter(ParserQueueJob.objects.filter(pk=job.pk, status=ParserQueueJob.STATUS_RUNNING), job, cutoff).update(
            status=ParserQueueJob.STATUS_FAILED,
            finished_at=now,
            error_message=STALE_JOB_MESSAGE,
        )
        recovered += updated
    return recovered


def recover_stale_batches(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=settings.PARSER_STALE_BATCH_MINUTES)
    recovered = 0
    for batch in ParserBatch.objects.filter(status=ParserBatch.STATUS_RUNNING):
        if not _is_stale(batch, cutoff):
            continue
        updated = _stale_filter(ParserBatch.objects.filter(pk=batch.pk, status=ParserBatch.STATUS_RUNNING), batch, cutoff).update(
            status=ParserBatch.STATUS_FAILED,
            finished_at=now,
            current_parser=None,
            log=_append_log(batch.log, STALE_BATCH_MESSAGE),
        )
        recovered += updated
    return recovered


def _is_stale(obj, cutoff):
    timestamp = obj.heartbeat_at or obj.started_at
    return timestamp is not None and timestamp < cutoff


def _stale_filter(queryset, obj, cutoff):
    if obj.heartbeat_at is not None:
        return queryset.filter(heartbeat_at__lt=cutoff)
    return queryset.filter(heartbeat_at__isnull=True, started_at__lt=cutoff)


def _append_log(current_log, message):
    return f"{current_log}\n{message}" if current_log else message
