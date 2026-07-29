from django.db import models

from .services.normalization import (
    build_search_text,
    normalize_brand,
    normalize_model,
    normalize_product_name,
    normalize_text,
)


class Shop(models.Model):
    name = models.CharField(max_length=150, unique=True)
    code = models.SlugField(max_length=50, unique=True)
    website_url = models.URLField(blank=True)
    logo_url = models.URLField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Category(models.Model):
    shop = models.ForeignKey(Shop, related_name="categories", on_delete=models.CASCADE)
    external_id = models.CharField(max_length=150)
    name = models.CharField(max_length=255)
    normalized_name = models.CharField(max_length=255, blank=True, db_index=True)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        related_name="children",
        on_delete=models.CASCADE,
    )
    url = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["shop", "external_id"],
                name="unique_category_external_id_per_shop",
            ),
        ]
        ordering = ["shop__name", "name"]

    def save(self, *args, **kwargs):
        self.normalized_name = normalize_text(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Product(models.Model):
    name = models.CharField(max_length=500)
    normalized_name = models.CharField(max_length=500, blank=True, db_index=True)
    brand = models.CharField(max_length=150, blank=True)
    normalized_brand = models.CharField(max_length=150, blank=True, db_index=True)
    model = models.CharField(max_length=200, blank=True)
    normalized_model = models.CharField(max_length=200, blank=True, db_index=True)
    barcode = models.CharField(max_length=50, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        self.normalized_name = normalize_product_name(self.name)
        self.normalized_brand = normalize_brand(self.brand)
        self.normalized_model = normalize_model(self.model)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class ProductOffer(models.Model):
    shop = models.ForeignKey(Shop, related_name="offers", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, related_name="offers", on_delete=models.CASCADE)
    category = models.ForeignKey(
        Category,
        related_name="offers",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    external_id = models.CharField(max_length=150)
    sku = models.CharField(max_length=150, blank=True, db_index=True)
    barcode = models.CharField(max_length=50, blank=True, db_index=True)
    original_name = models.CharField(max_length=500)
    normalized_name = models.CharField(max_length=500, blank=True, db_index=True)
    search_text = models.TextField(blank=True)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    sale_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="EUR")
    product_url = models.URLField(max_length=1000, blank=True)
    image_url = models.URLField(max_length=1000, blank=True)
    is_available = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["shop", "external_id"],
                name="unique_offer_external_id_per_shop",
            ),
        ]
        indexes = [
            models.Index(fields=["shop", "normalized_name"]),
            models.Index(fields=["shop", "barcode"]),
            models.Index(fields=["shop", "sku"]),
            models.Index(fields=["is_active", "is_available"]),
        ]
        ordering = ["shop__name", "original_name"]

    @property
    def current_price(self):
        return self.sale_price if self.sale_price is not None else self.price

    def save(self, *args, **kwargs):
        self.normalized_name = normalize_product_name(self.original_name)
        category_name = self.category.name if self.category_id and self.category else ""
        self.search_text = build_search_text(
            self.original_name,
            self.normalized_name,
            self.sku,
            self.barcode,
            self.product.brand if self.product_id and self.product else "",
            self.product.model if self.product_id and self.product else "",
            category_name,
            self.shop.name if self.shop_id and self.shop else "",
        )
        super().save(*args, **kwargs)

    def __str__(self):
        return self.original_name


class PriceHistory(models.Model):
    offer = models.ForeignKey(
        ProductOffer,
        related_name="price_history",
        on_delete=models.CASCADE,
    )
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    sale_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-recorded_at"]

    def __str__(self):
        return f"{self.offer} at {self.recorded_at:%Y-%m-%d %H:%M:%S}"

# Create your models here.
