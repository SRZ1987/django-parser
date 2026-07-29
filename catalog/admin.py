from django.contrib import admin

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
    list_display = ("name", "shop", "external_id", "parent", "created_at", "updated_at")
    search_fields = ("name", "normalized_name", "external_id", "shop__name")
    list_filter = ("shop",)
    autocomplete_fields = ("shop", "parent")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("shop__name", "name")
    list_per_page = 50


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "brand", "model", "barcode", "created_at", "updated_at")
    search_fields = (
        "name",
        "normalized_name",
        "brand",
        "normalized_brand",
        "model",
        "normalized_model",
        "barcode",
    )
    list_filter = ("brand",)
    readonly_fields = ("created_at", "updated_at")
    ordering = ("name",)
    list_per_page = 50


@admin.register(ProductOffer)
class ProductOfferAdmin(admin.ModelAdmin):
    list_display = (
        "original_name",
        "shop",
        "product",
        "sku",
        "barcode",
        "current_price",
        "currency",
        "is_available",
        "is_active",
        "updated_at",
    )
    search_fields = ("original_name", "normalized_name", "sku", "barcode")
    list_filter = ("shop", "category", "is_active", "is_available", "currency")
    autocomplete_fields = ("shop", "product", "category")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("shop__name", "original_name")
    list_per_page = 50


@admin.register(PriceHistory)
class PriceHistoryAdmin(admin.ModelAdmin):
    list_display = ("offer", "price", "sale_price", "recorded_at")
    search_fields = ("offer__original_name", "offer__sku", "offer__barcode")
    list_filter = ("offer__shop",)
    autocomplete_fields = ("offer",)
    readonly_fields = ("recorded_at",)
    ordering = ("-recorded_at",)
    list_per_page = 50

# Register your models here.
