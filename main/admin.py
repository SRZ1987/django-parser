from django.contrib import admin

from .models import ShoppingList, ShoppingListItem


class ShoppingListItemInline(admin.TabularInline):
    model = ShoppingListItem
    extra = 0
    autocomplete_fields = ["product", "source_offer"]
    readonly_fields = ["created_at"]


@admin.register(ShoppingList)
class ShoppingListAdmin(admin.ModelAdmin):
    list_display = ["user", "items_count", "updated_at"]
    search_fields = ["user__username", "user__email"]
    readonly_fields = ["created_at", "updated_at"]
    inlines = [ShoppingListItemInline]

    def items_count(self, obj):
        return obj.items.count()


@admin.register(ShoppingListItem)
class ShoppingListItemAdmin(admin.ModelAdmin):
    list_display = ["name", "shopping_list", "source_offer", "is_purchased", "created_at"]
    list_filter = ["is_purchased"]
    search_fields = ["name", "shopping_list__user__username", "source_offer__original_name"]
    autocomplete_fields = ["shopping_list", "product", "source_offer"]
    readonly_fields = ["created_at"]
