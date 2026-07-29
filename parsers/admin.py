from django.contrib import admin

from .models import ParserConfig, ParserRun


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
        "parser",
        "status",
        "trigger",
        "started_at",
        "finished_at",
        "products_found",
        "products_created",
        "products_updated",
        "prices_changed",
        "errors_count",
        "created_at",
    )
    search_fields = ("parser__name", "parser__code", "error_message", "log")
    list_filter = ("status", "trigger", "parser")
    readonly_fields = tuple(field.name for field in ParserRun._meta.fields)
    ordering = ("-created_at",)
    list_per_page = 50

    def has_add_permission(self, request):
        return False

# Register your models here.
