import traceback

from django.db import transaction
from django.utils import timezone

from parsers.models import ParserConfig, ParserRun

from .base import ParserAlreadyRunningError, ParserError
from .registry import get_parser_class


def run_parser(parser_code: str, trigger: str = "command", force: bool = False) -> ParserRun:
    parser_config, parser_run = _start_parser_run(parser_code, trigger, force)

    try:
        parser_class = get_parser_class(parser_config.code)
        result = parser_class(parser_config, parser_run).run()
    except Exception as exc:
        _finish_failed_run(parser_config.pk, parser_run.pk, exc)
        return ParserRun.objects.get(pk=parser_run.pk)

    _finish_success_run(parser_config.pk, parser_run.pk, result)
    return ParserRun.objects.get(pk=parser_run.pk)


def _start_parser_run(parser_code, trigger, force):
    now = timezone.now()

    with transaction.atomic():
        try:
            parser_config = ParserConfig.objects.select_for_update().get(code=parser_code)
        except ParserConfig.DoesNotExist as exc:
            raise ParserError(f"Parser config '{parser_code}' was not found.") from exc

        if not parser_config.is_enabled and not force:
            raise ParserError(f"Parser '{parser_code}' is disabled.")

        if parser_config.is_running:
            raise ParserAlreadyRunningError(f"Parser '{parser_code}' is already running.")

        parser_config.is_running = True
        parser_config.last_started_at = now
        parser_config.last_status = ParserConfig.STATUS_RUNNING
        parser_config.last_error = ""
        parser_config.save(
            update_fields=[
                "is_running",
                "last_started_at",
                "last_status",
                "last_error",
                "updated_at",
            ]
        )

        parser_run = ParserRun.objects.create(
            parser=parser_config,
            status=ParserRun.STATUS_RUNNING,
            trigger=trigger,
            started_at=now,
        )

    return parser_config, parser_run


def _finish_success_run(parser_config_id, parser_run_id, result):
    now = timezone.now()

    with transaction.atomic():
        parser_config = ParserConfig.objects.select_for_update().get(pk=parser_config_id)
        parser_run = ParserRun.objects.select_for_update().get(pk=parser_run_id)

        parser_run.status = ParserRun.STATUS_SUCCESS
        parser_run.finished_at = now
        parser_run.products_found = result.products_found
        parser_run.products_created = result.products_created
        parser_run.products_updated = result.products_updated
        parser_run.prices_changed = result.prices_changed
        parser_run.errors_count = result.errors_count
        parser_run.save(
            update_fields=[
                "status",
                "finished_at",
                "products_found",
                "products_created",
                "products_updated",
                "prices_changed",
                "errors_count",
            ]
        )

        parser_config.is_running = False
        parser_config.last_finished_at = now
        parser_config.last_success_at = now
        parser_config.last_status = ParserConfig.STATUS_SUCCESS
        parser_config.last_error = ""
        parser_config.save(
            update_fields=[
                "is_running",
                "last_finished_at",
                "last_success_at",
                "last_status",
                "last_error",
                "updated_at",
            ]
        )


def _finish_failed_run(parser_config_id, parser_run_id, exc):
    now = timezone.now()
    error_message = str(exc)
    error_traceback = traceback.format_exc()

    with transaction.atomic():
        parser_config = ParserConfig.objects.select_for_update().get(pk=parser_config_id)
        parser_run = ParserRun.objects.select_for_update().get(pk=parser_run_id)

        parser_run.status = ParserRun.STATUS_FAILED
        parser_run.finished_at = now
        parser_run.errors_count = max(parser_run.errors_count, 1)
        parser_run.error_message = error_message
        parser_run.log = _append_traceback(parser_run.log, error_traceback)
        parser_run.save(
            update_fields=[
                "status",
                "finished_at",
                "errors_count",
                "error_message",
                "log",
            ]
        )

        parser_config.is_running = False
        parser_config.last_finished_at = now
        parser_config.last_status = ParserConfig.STATUS_FAILED
        parser_config.last_error = error_traceback
        parser_config.save(
            update_fields=[
                "is_running",
                "last_finished_at",
                "last_status",
                "last_error",
                "updated_at",
            ]
        )


def _append_traceback(current_log, error_traceback):
    if not current_log:
        return error_traceback
    return f"{current_log}\n{error_traceback}"
