from django.contrib import admin

from .models import ParserConfig, ParserRun


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
        "run_time",
        "is_running",
        "last_status",
        "last_started_at",
        "last_success_at",
    )
    search_fields = ("name", "code", "shop__name")
    list_filter = ("is_enabled", "is_running", "last_status")
    autocomplete_fields = ("shop",)
    readonly_fields = (
        "is_running",
        "last_started_at",
        "last_finished_at",
        "last_success_at",
        "last_status",
        "last_error",
        "created_at",
        "updated_at",
    )
    ordering = ("name",)
    list_per_page = 50


@admin.register(ParserRun)
class ParserRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "parser_config",
        "status",
        "products_found",
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
        "trigger",
        "products_found",
        "products_created",
        "products_updated",
        "prices_changed",
        "errors_count",
        "log",
        "error_message",
        "started_at",
        "finished_at",
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

# Register your models here.
