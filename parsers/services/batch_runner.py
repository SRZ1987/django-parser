import os
import threading

from django.core.files import File
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from parsers.adapters.registry import ADAPTERS, get_adapter_class
from parsers.models import ParserBatch, ParserBatchLock, ParserConfig, ParserExport, ParserQueueJob, ParserRun

from .excel_importer import ExcelCatalogImporter
from .excel_validation import ExcelCatalogValidator
from .export_storage import export_work_paths
from .heartbeat import HeartbeatTicker
from .recovery import recover_stale_parser_state


class ParserBatchAlreadyRunning(Exception):
    pass


class ParserBatchLockMissing(Exception):
    pass


class ParserAlreadyRunning(Exception):
    pass


class ParserCancelled(Exception):
    pass


def run_all_parsers(trigger=ParserRun.TRIGGER_COMMAND, force=False):
    recover_stale_parser_state()
    batch = start_batch(trigger=trigger, force=force)
    configs = ParserConfig.objects.filter(is_enabled=True, code__in=ADAPTERS.keys()).order_by("run_order", "name")
    any_failed = False

    with HeartbeatTicker([(ParserBatch, batch.pk, ParserBatch.STATUS_RUNNING)]):
        try:
            for parser_config in configs:
                batch.current_parser = parser_config
                batch.heartbeat_at = timezone.now()
                batch.save(update_fields=["current_parser", "heartbeat_at"])
                run = run_excel_parser(parser_config, trigger=trigger)
                if run.status != ParserRun.STATUS_SUCCESS:
                    any_failed = True

            batch.status = ParserBatch.STATUS_PARTIAL if any_failed else ParserBatch.STATUS_SUCCESS
            return batch
        except Exception as exc:
            batch.status = ParserBatch.STATUS_FAILED
            batch.log = append_log(batch.log, str(exc))
            raise
        finally:
            batch.finished_at = timezone.now()
            batch.current_parser = None
            batch.save(update_fields=["status", "finished_at", "current_parser", "log"])


def start_batch(trigger, force=False):
    try:
        recover_stale_parser_state()
    except OperationalError as exc:
        if "locked" in str(exc).lower():
            raise ParserBatchAlreadyRunning("Parser batch is already running.") from exc
        raise
    now = timezone.now()
    try:
        with transaction.atomic():
            try:
                ParserBatchLock.objects.select_for_update().get(name="nightly_parser_batch")
            except ParserBatchLock.DoesNotExist as exc:
                raise ParserBatchLockMissing(
                    "Parser batch lock is missing. Run migrations to create the 'nightly_parser_batch' lock row."
                ) from exc
            running = ParserBatch.objects.filter(status=ParserBatch.STATUS_RUNNING).first()
            if running and not force:
                raise ParserBatchAlreadyRunning("Parser batch is already running.")
            if running and force:
                raise ParserBatchAlreadyRunning("Cannot force a second parser batch while another batch is running.")
            return ParserBatch.objects.create(status=ParserBatch.STATUS_RUNNING, trigger=trigger, started_at=now, heartbeat_at=now)
    except IntegrityError as exc:
        raise ParserBatchAlreadyRunning("Parser batch is already running.") from exc
    except OperationalError as exc:
        if "locked" in str(exc).lower():
            raise ParserBatchAlreadyRunning("Parser batch is already running.") from exc
        raise


def run_excel_parser(parser_config, trigger=ParserRun.TRIGGER_COMMAND):
    adapter_class = get_adapter_class(parser_config.code)
    if adapter_class is None:
        return _failed_run(parser_config, trigger, f"No Excel adapter registered for parser '{parser_config.code}'.")

    try:
        with transaction.atomic():
            parser_run = ParserRun.objects.create(
                parser=parser_config,
                status=ParserRun.STATUS_RUNNING,
                stage=ParserRun.STAGE_PARSING,
                trigger=trigger,
                started_at=timezone.now(),
                heartbeat_at=timezone.now(),
            )
    except IntegrityError as exc:
        error = ParserAlreadyRunning(f"Parser '{parser_config.code}' is already running.")
        return _failed_run(parser_config, trigger, str(error))

    adapter = adapter_class()

    adapter_logs = []
    adapter_logs_lock = threading.Lock()
    stop_log_flusher = threading.Event()

    def log(message):
        with adapter_logs_lock:
            adapter_logs.append(str(message))

    def flush_logs(force=False):
        with adapter_logs_lock:
            if not adapter_logs:
                return
            message = "\n".join(adapter_logs)
            adapter_logs.clear()
        parser_run.log = append_log(parser_run.log, message)
        parser_run.heartbeat_at = timezone.now()
        parser_run.save(update_fields=["log", "heartbeat_at"])

    def flush_logs_periodically():
        while not stop_log_flusher.wait(15):
            flush_logs()

    tmp_path, final_path = export_work_paths(parser_config.code)
    log_flusher = threading.Thread(target=flush_logs_periodically, daemon=True)
    log_flusher.start()
    with HeartbeatTicker([(ParserRun, parser_run.pk, ParserRun.STATUS_RUNNING)]):
        try:
            result = _run_adapter(adapter, tmp_path, log)
            stop_log_flusher.set()
            log_flusher.join(timeout=5)
            flush_logs(force=True)
            if not result.success:
                raise RuntimeError(result.error_message or "Parser did not create Excel export.")
            os.replace(tmp_path, final_path)

            _check_cancel_requested(parser_run)
            parser_run.stage = ParserRun.STAGE_EXCEL_VALIDATION
            parser_run.save(update_fields=["stage"])
            _check_cancel_requested(parser_run)
            validation = ExcelCatalogValidator().validate(final_path, column_map=adapter.column_map, worksheet_name=adapter.worksheet_name)
            if not validation.is_valid:
                raise RuntimeError(validation.error_message)

            parser_export = _create_parser_export(parser_run, final_path, validation.rows_count)
            parser_run.excel_rows_count = validation.rows_count
            parser_run.stage = ParserRun.STAGE_DATABASE_IMPORT
            parser_run.save(update_fields=["excel_rows_count", "stage"])

            _check_cancel_requested(parser_run)
            import_result = ExcelCatalogImporter().import_file(
                parser_export,
                column_map=adapter.column_map,
                worksheet_name=adapter.worksheet_name,
                parser_run=parser_run,
            )
            parser_run.products_found = import_result.products_found
            parser_run.products_created = import_result.products_created
            parser_run.products_updated = import_result.products_updated
            parser_run.prices_changed = import_result.prices_changed
            parser_run.skipped_rows = import_result.skipped_rows
            parser_run.errors_count = import_result.errors_count
            parser_run.status = ParserRun.STATUS_SUCCESS
            parser_run.stage = ParserRun.STAGE_COMPLETED
            parser_run.finished_at = timezone.now()
            parser_run.save()
            return parser_run
        except ParserCancelled as exc:
            stop_log_flusher.set()
            log_flusher.join(timeout=5)
            flush_logs(force=True)
            parser_run.status = ParserRun.STATUS_CANCELLED
            parser_run.error_message = str(exc)
            parser_run.finished_at = timezone.now()
            parser_run.save()
            return parser_run
        except Exception as exc:
            stop_log_flusher.set()
            log_flusher.join(timeout=5)
            flush_logs(force=True)
            parser_run.status = ParserRun.STATUS_FAILED
            parser_run.error_message = str(exc)
            parser_run.errors_count = max(parser_run.errors_count, 1)
            parser_run.finished_at = timezone.now()
            parser_run.save()
            return parser_run


def process_next_queue_job():
    recover_stale_parser_state()
    with transaction.atomic():
        job = ParserQueueJob.objects.select_for_update(skip_locked=True).filter(status=ParserQueueJob.STATUS_PENDING).first()
        if job is None:
            return None
        job.status = ParserQueueJob.STATUS_RUNNING
        job.attempts += 1
        job.started_at = timezone.now()
        job.heartbeat_at = timezone.now()
        job.save(update_fields=["status", "attempts", "started_at", "heartbeat_at"])

    with HeartbeatTicker([(ParserQueueJob, job.pk, ParserQueueJob.STATUS_RUNNING)]):
        try:
            if job.run_all:
                batch = run_all_parsers(trigger=job.trigger)
                job.batch = batch
                job.status = ParserQueueJob.STATUS_SUCCESS
            else:
                run = run_excel_parser(job.parser_config, trigger=job.trigger)
                job.parser_run = run
                job.status = ParserQueueJob.STATUS_SUCCESS if run.status == ParserRun.STATUS_SUCCESS else ParserQueueJob.STATUS_FAILED
                job.error_message = run.error_message
        except Exception as exc:
            job.status = ParserQueueJob.STATUS_FAILED
            job.error_message = str(exc)
        finally:
            job.finished_at = timezone.now()
            job.save(update_fields=["batch", "parser_run", "status", "error_message", "finished_at"])
    return job


def _run_adapter(adapter, tmp_path, log):
    import asyncio

    return asyncio.run(adapter.run(tmp_path, log_callback=log))


def _create_parser_export(parser_run, final_path, rows_count):
    parser_export = ParserExport(
        parser_run=parser_run,
        shop=parser_run.parser.shop,
        original_filename=final_path.name,
        rows_count=rows_count,
        file_size=final_path.stat().st_size,
    )
    with open(final_path, "rb") as handle:
        parser_export.file.save(final_path.name, File(handle), save=True)
    return parser_export


def _failed_run(parser_config, trigger, message):
    return ParserRun.objects.create(
        parser=parser_config,
        status=ParserRun.STATUS_FAILED,
        stage=ParserRun.STAGE_COMPLETED,
        trigger=trigger,
        started_at=timezone.now(),
        finished_at=timezone.now(),
        errors_count=1,
        error_message=message,
    )


def append_log(current_log, message):
    return f"{current_log}\n{message}" if current_log else message


def _check_cancel_requested(parser_run):
    parser_run.refresh_from_db(fields=["cancel_requested"])
    if parser_run.cancel_requested:
        raise ParserCancelled("Parser run was cancelled before the next stage.")
