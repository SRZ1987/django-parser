from django.contrib import admin
from django.utils.html import format_html

from .models import Category, PriceHistory, Product, ProductOffer, Shop


@admin.register(Shop)
class ShopAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_active", "website_url", "created_at", "updated_at")
    search_fields = ("name", "code", "website_url")
    list_filter = ("is_active",)
    readonly_fields = ("created_at", "updated_at")
    ordering = ("name",)
    list_per_page = 50


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "shop", "parent", "external_id")
    search_fields = ("name", "external_id")
    list_filter = ("shop",)
    list_select_related = ("shop", "parent")
    autocomplete_fields = ("shop", "parent")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("shop__name", "name")
    list_per_page = 50


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "brand", "model", "barcode", "updated_at")
    search_fields = ("name", "brand", "model", "barcode")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("name",)
    list_per_page = 50


@admin.register(ProductOffer)
class ProductOfferAdmin(admin.ModelAdmin):
    list_display = (
        "original_name",
        "shop",
        "category",
        "sku",
        "barcode",
        "price",
        "sale_price",
        "quantity_price",
        "quantity_price_min_quantity",
        "currency",
        "is_available",
        "is_active",
        "last_seen_at",
        "image_preview",
        "product_link",
    )
    search_fields = (
        "original_name",
        "sku",
        "barcode",
        "external_id",
        "product__name",
        "product__brand",
        "product__model",
    )
    list_filter = ("shop", "is_available", "is_active", "currency")
    list_select_related = ("shop", "category", "product")
    autocomplete_fields = ("shop", "product", "category")
    readonly_fields = ("image_preview", "product_link", "created_at", "updated_at", "last_seen_at")
    ordering = ("-updated_at",)
    list_per_page = 50
    date_hierarchy = "last_seen_at"
    actions = (
        "mark_active",
        "mark_inactive",
        "mark_available",
        "mark_unavailable",
    )

    @admin.display(description="Image")
    def image_preview(self, obj):
        if not obj.image_url:
            return "-"
        return format_html(
            '<img src="{}" alt="{}" width="60" height="60" style="object-fit:contain;border-radius:8px;" />',
            obj.image_url,
            obj.original_name,
        )

    @admin.display(description="Product link")
    def product_link(self, obj):
        if not obj.product_url:
            return "-"
        return format_html('<a href="{}" target="_blank" rel="noopener noreferrer">Открыть</a>', obj.product_url)

    @admin.action(description="Отметить выбранные предложения активными")
    def mark_active(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"Активными отмечено: {updated}.")

    @admin.action(description="Отметить выбранные предложения неактивными")
    def mark_inactive(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"Неактивными отмечено: {updated}.")

    @admin.action(description="Отметить выбранные предложения доступными")
    def mark_available(self, request, queryset):
        updated = queryset.update(is_available=True)
        self.message_user(request, f"Доступными отмечено: {updated}.")

    @admin.action(description="Отметить выбранные предложения недоступными")
    def mark_unavailable(self, request, queryset):
        updated = queryset.update(is_available=False)
        self.message_user(request, f"Недоступными отмечено: {updated}.")


@admin.register(PriceHistory)
class PriceHistoryAdmin(admin.ModelAdmin):
    list_display = (
        "offer",
        "price",
        "sale_price",
        "quantity_price",
        "quantity_price_min_quantity",
        "recorded_at",
    )
    search_fields = ("offer__original_name", "offer__sku", "offer__barcode")
    list_filter = ("offer__shop",)
    autocomplete_fields = ("offer",)
    readonly_fields = ("recorded_at",)
    ordering = ("-recorded_at",)
    list_per_page = 50

# Register your models here.
