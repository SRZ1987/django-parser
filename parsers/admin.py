from django.contrib import admin, messages
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils.html import format_html

from .models import ParserBatch, ParserConfig, ParserExport, ParserQueueJob, ParserRun


class ParserConfigListFilter(admin.SimpleListFilter):
    title = "parser_config"
    parameter_name = "parser_config"

    def lookups(self, request, model_admin):
        return ParserConfig.objects.order_by("name").values_list("id", "name")

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(parser_id=self.value())
        return queryset


@admin.register(ParserConfig)
class ParserConfigAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "code",
        "shop",
        "is_enabled",
        "run_order",
        "run_time",
        "is_running",
        "last_status",
        "last_started_at",
        "last_success_at",
    )
    search_fields = ("name", "code", "shop__name")
    list_filter = ("is_enabled", "is_running", "last_status")
    autocomplete_fields = ("shop",)
    actions = ("queue_selected_parsers", "queue_all_parsers")
    readonly_fields = (
        "is_running",
        "last_started_at",
        "last_finished_at",
        "last_success_at",
        "last_status",
        "last_error",
        "runtime_settings",
        "created_at",
        "updated_at",
    )
    ordering = ("run_order", "name")
    list_per_page = 50

    @admin.action(description="Queue selected parsers")
    def queue_selected_parsers(self, request, queryset):
        created = 0
        for parser_config in queryset:
            ParserQueueJob.objects.create(parser_config=parser_config, trigger=ParserRun.TRIGGER_ADMIN)
            created += 1
        self.message_user(request, f"Queued parser jobs: {created}", messages.SUCCESS)

    @admin.action(description="Queue all parsers")
    def queue_all_parsers(self, request, queryset):
        ParserQueueJob.objects.create(run_all=True, trigger=ParserRun.TRIGGER_ADMIN)
        self.message_user(request, "Parser batch job queued.", messages.SUCCESS)


@admin.register(ParserRun)
class ParserRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "parser_config",
        "status",
        "stage",
        "products_found",
        "excel_rows_count",
        "products_created",
        "products_updated",
        "prices_changed",
        "errors_count",
        "started_at",
        "finished_at",
        "duration",
    )
    list_filter = ("status", ParserConfigListFilter)
    search_fields = ("parser__name", "parser__code", "error_message")
    readonly_fields = (
        "parser",
        "status",
        "stage",
        "trigger",
        "excel_rows_count",
        "products_found",
        "products_created",
        "products_updated",
        "prices_changed",
        "errors_count",
        "skipped_rows",
        "log",
        "error_message",
        "started_at",
        "finished_at",
        "heartbeat_at",
        "cancel_requested",
        "created_at",
    )
    list_select_related = ("parser",)
    ordering = ("-created_at",)
    list_per_page = 50

    @admin.display(description="Parser config", ordering="parser__name")
    def parser_config(self, obj):
        return obj.parser

    @admin.display(description="Duration")
    def duration(self, obj):
        if not obj.started_at or not obj.finished_at:
            return "-"

        total_seconds = int((obj.finished_at - obj.started_at).total_seconds())
        if total_seconds < 0:
            return "-"

        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)

        if hours:
            return f"{hours}h {minutes}m {seconds}s"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    def has_add_permission(self, request):
        return False


@admin.register(ParserExport)
class ParserExportAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "shop",
        "original_filename",
        "rows_count",
        "file_size",
        "import_success",
        "parser_run_link",
        "download_link",
        "created_at",
        "imported_at",
    )
    list_filter = ("shop", "import_success", "created_at", "parser_run__status")
    search_fields = ("original_filename", "shop__name", "parser_run__parser__code")
    readonly_fields = (
        "parser_run",
        "shop",
        "file",
        "original_filename",
        "rows_count",
        "file_size",
        "created_at",
        "imported_at",
        "import_success",
        "validation_error",
        "download_link",
    )
    list_select_related = ("shop", "parser_run", "parser_run__parser")
    ordering = ("-created_at",)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:object_id>/download/",
                self.admin_site.admin_view(self.download_view),
                name="parsers_parserexport_download",
            )
        ]
        return custom_urls + urls

    def download_view(self, request, object_id):
        if not request.user.has_perm("parsers.view_parserexport"):
            raise Http404
        parser_export = get_object_or_404(ParserExport, pk=object_id)
        if not parser_export.file:
            raise Http404
        return FileResponse(
            parser_export.file.open("rb"),
            as_attachment=True,
            filename=parser_export.original_filename,
        )

    @admin.display(description="Parser run")
    def parser_run_link(self, obj):
        url = reverse("admin:parsers_parserrun_change", args=[obj.parser_run_id])
        return format_html('<a href="{}">#{}</a>', url, obj.parser_run_id)

    @admin.display(description="Download")
    def download_link(self, obj):
        url = reverse("admin:parsers_parserexport_download", args=[obj.pk])
        return format_html('<a href="{}">Download</a>', url)

    def has_add_permission(self, request):
        return False


@admin.register(ParserBatch)
class ParserBatchAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "trigger", "current_parser", "started_at", "finished_at", "heartbeat_at")
    list_filter = ("status", "trigger")
    readonly_fields = ("status", "trigger", "started_at", "finished_at", "current_parser", "heartbeat_at", "log", "created_at")
    list_select_related = ("current_parser",)
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False


@admin.register(ParserQueueJob)
class ParserQueueJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "parser_config",
        "run_all",
        "status",
        "trigger",
        "attempts",
        "started_at",
        "finished_at",
        "created_at",
    )
    list_filter = ("status", "trigger", "run_all")
    search_fields = ("parser_config__name", "parser_config__code", "error_message")
    readonly_fields = (
        "parser_config",
        "batch",
        "parser_run",
        "status",
        "trigger",
        "run_all",
        "attempts",
        "started_at",
        "finished_at",
        "heartbeat_at",
        "error_message",
        "log",
        "created_at",
    )
    list_select_related = ("parser_config", "batch", "parser_run")
    ordering = ("created_at",)

    def has_add_permission(self, request):
        return False

# Register your models here.
