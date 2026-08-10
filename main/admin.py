from django.contrib import admin

from .models import DailySiteVisit, ShoppingList, ShoppingListEvent, ShoppingListItem, StoreClick


class ReadOnlyAnalyticsAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ShoppingListItemInline(admin.TabularInline):
    model = ShoppingListItem
    extra = 0
    autocomplete_fields = ["product", "source_offer"]
    readonly_fields = [
        "price_alert_source_price",
        "price_alert_best_price",
        "price_alert_best_offer",
        "price_alert_checked_at",
        "created_at",
    ]


@admin.register(ShoppingList)
class ShoppingListAdmin(admin.ModelAdmin):
    list_display = ["user", "items_count", "price_alerts_enabled", "price_alerts_last_sent_at", "updated_at"]
    list_filter = ["price_alerts_enabled"]
    search_fields = ["user__username", "user__email"]
    readonly_fields = ["share_token", "price_alerts_enabled_at", "price_alerts_last_sent_at", "created_at", "updated_at"]
    inlines = [ShoppingListItemInline]

    def items_count(self, obj):
        return obj.items.count()


@admin.register(ShoppingListItem)
class ShoppingListItemAdmin(admin.ModelAdmin):
    list_display = ["name", "shopping_list", "source_offer", "is_purchased", "created_at"]
    list_filter = ["is_purchased"]
    search_fields = ["name", "shopping_list__user__username", "source_offer__original_name"]
    autocomplete_fields = ["shopping_list", "product", "source_offer"]
    readonly_fields = [
        "price_alert_source_price",
        "price_alert_best_price",
        "price_alert_best_offer",
        "price_alert_checked_at",
        "created_at",
    ]


@admin.register(DailySiteVisit)
class DailySiteVisitAdmin(ReadOnlyAnalyticsAdmin):
    list_display = ["date", "visitor_hash_short", "user", "pageviews", "last_path", "last_seen_at"]
    list_filter = ["date"]
    search_fields = ["visitor_hash", "user__username", "user__email", "first_path", "last_path"]
    list_select_related = ["user"]
    date_hierarchy = "date"
    ordering = ["-date", "-last_seen_at"]
    readonly_fields = [
        "date",
        "visitor_hash",
        "user",
        "first_path",
        "last_path",
        "pageviews",
        "first_seen_at",
        "last_seen_at",
    ]

    @admin.display(description="Visitor")
    def visitor_hash_short(self, obj):
        return obj.visitor_hash[:12]


@admin.register(StoreClick)
class StoreClickAdmin(ReadOnlyAnalyticsAdmin):
    list_display = ["clicked_at", "shop", "offer", "user", "source_path"]
    list_filter = ["shop", "clicked_at"]
    search_fields = ["offer__original_name", "user__username", "user__email", "visitor_hash"]
    list_select_related = ["shop", "offer", "user"]
    date_hierarchy = "clicked_at"
    ordering = ["-clicked_at"]
    readonly_fields = ["shop", "offer", "user", "visitor_hash", "source_path", "clicked_at"]


@admin.register(ShoppingListEvent)
class ShoppingListEventAdmin(ReadOnlyAnalyticsAdmin):
    list_display = ["created_at", "event_type", "item_name", "shop", "user"]
    list_filter = ["event_type", "shop", "created_at"]
    search_fields = ["item_name", "offer__original_name", "user__username", "user__email"]
    list_select_related = ["shop", "offer", "user"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]
    readonly_fields = ["event_type", "user", "shop", "offer", "item_name", "created_at"]
