from django.contrib import admin
from django.db.models import Count, Q

from .models import (
    DailySiteVisit,
    GroupPurchase,
    GroupPurchaseMember,
    GroupPurchaseMessage,
    SearchQueryLog,
    ShoppingList,
    ShoppingListEvent,
    ShoppingListItem,
    StoreClick,
)


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


class GroupPurchaseMemberInline(admin.TabularInline):
    model = GroupPurchaseMember
    extra = 0
    autocomplete_fields = ["user", "shopping_list_item"]
    readonly_fields = ["joined_at"]


class GroupPurchaseMessageInline(admin.TabularInline):
    model = GroupPurchaseMessage
    extra = 0
    autocomplete_fields = ["sender"]
    readonly_fields = ["created_at"]


@admin.register(GroupPurchase)
class GroupPurchaseAdmin(admin.ModelAdmin):
    list_display = [
        "offer",
        "status",
        "target_quantity",
        "quantity_price",
        "members_count",
        "last_activity_at",
    ]
    list_filter = ["status", "offer__shop"]
    search_fields = ["offer__original_name", "offer__sku", "offer__barcode"]
    list_select_related = ["offer", "offer__shop"]
    readonly_fields = ["created_at", "updated_at", "last_activity_at", "closed_at"]
    autocomplete_fields = ["offer"]
    inlines = [GroupPurchaseMemberInline, GroupPurchaseMessageInline]

    @admin.display(description="Participants")
    def members_count(self, obj):
        return obj.members.count()


@admin.register(GroupPurchaseMessage)
class GroupPurchaseMessageAdmin(admin.ModelAdmin):
    list_display = ["group", "sender", "body_preview", "created_at"]
    search_fields = ["body", "sender__username", "sender__email", "group__offer__original_name"]
    list_select_related = ["group", "group__offer", "sender"]
    readonly_fields = ["created_at"]
    autocomplete_fields = ["group", "sender"]

    @admin.display(description="Message")
    def body_preview(self, obj):
        return obj.body[:80]


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


@admin.register(SearchQueryLog)
class SearchQueryLogAdmin(ReadOnlyAnalyticsAdmin):
    change_list_template = "admin/main/searchquerylog/change_list.html"
    list_display = [
        "searched_at",
        "query",
        "results_count",
        "has_results",
        "source",
        "user",
        "visitor_hash_short",
        "language_code",
    ]
    list_filter = ["has_results", "source", "language_code", "searched_at"]
    search_fields = [
        "query",
        "normalized_query",
        "visitor_hash",
        "user__username",
        "user__email",
    ]
    list_select_related = ["user"]
    date_hierarchy = "searched_at"
    ordering = ["-searched_at"]
    readonly_fields = [
        "query",
        "normalized_query",
        "user",
        "visitor_hash",
        "source",
        "language_code",
        "results_count",
        "candidates_count",
        "has_results",
        "searched_at",
    ]

    @admin.display(description="Посетитель")
    def visitor_hash_short(self, obj):
        return obj.visitor_hash[:12]

    def changelist_view(self, request, extra_context=None):
        response = super().changelist_view(request, extra_context)
        if hasattr(response, "context_data"):
            queryset = response.context_data["cl"].queryset
            response.context_data["search_log_summary"] = queryset.aggregate(
                searches=Count("id"),
                visitors=Count("visitor_hash", distinct=True),
                registered_users=Count("user", distinct=True),
                no_results=Count("id", filter=Q(has_results=False)),
            )
        return response


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
