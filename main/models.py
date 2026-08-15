import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class ShoppingList(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        related_name="shopping_list",
        on_delete=models.CASCADE,
    )
    share_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    price_alerts_enabled = models.BooleanField(default=False)
    price_alerts_enabled_at = models.DateTimeField(null=True, blank=True)
    price_alerts_last_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user__username"]

    def __str__(self):
        return f"Shopping list for {self.user}"


class ShoppingListItem(models.Model):
    shopping_list = models.ForeignKey(
        ShoppingList,
        related_name="items",
        on_delete=models.CASCADE,
    )
    product = models.ForeignKey(
        "catalog.Product",
        related_name="shopping_list_items",
        on_delete=models.CASCADE,
    )
    source_offer = models.ForeignKey(
        "catalog.ProductOffer",
        related_name="shopping_list_items",
        on_delete=models.CASCADE,
    )
    name = models.CharField(max_length=500)
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(9999)],
    )
    is_purchased = models.BooleanField(default=False)
    price_alert_source_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    price_alert_best_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    price_alert_best_offer = models.ForeignKey(
        "catalog.ProductOffer",
        related_name="price_alert_snapshots",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    price_alert_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["shopping_list", "source_offer"],
                name="unique_shopping_list_source_offer",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1, quantity__lte=9999),
                name="shopping_list_item_quantity_range",
            ),
        ]
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class GroupPurchase(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Открыта"
        CLOSED = "closed", "Закрыта"
        EXPIRED = "expired", "Истекла"

    offer = models.ForeignKey(
        "catalog.ProductOffer",
        related_name="group_purchases",
        on_delete=models.CASCADE,
    )
    target_quantity = models.PositiveIntegerField()
    quantity_price = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    last_activity_at = models.DateTimeField(default=timezone.now, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["offer"],
                condition=models.Q(status="open"),
                name="unique_open_group_purchase_per_offer",
            ),
        ]
        ordering = ["-last_activity_at", "-id"]

    def __str__(self):
        return f"{self.offer} ({self.get_status_display()})"


class GroupPurchaseMember(models.Model):
    group = models.ForeignKey(
        GroupPurchase,
        related_name="members",
        on_delete=models.CASCADE,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="group_purchase_memberships",
        on_delete=models.CASCADE,
    )
    shopping_list_item = models.OneToOneField(
        ShoppingListItem,
        related_name="group_purchase_membership",
        on_delete=models.CASCADE,
    )
    quantity = models.PositiveIntegerField(default=1)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["group", "user"],
                name="unique_group_purchase_member",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1),
                name="group_purchase_member_quantity_gte_1",
            ),
        ]
        ordering = ["joined_at", "id"]

    def __str__(self):
        return f"{self.user} in {self.group}"


class GroupPurchaseMessage(models.Model):
    group = models.ForeignKey(
        GroupPurchase,
        related_name="messages",
        on_delete=models.CASCADE,
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="group_purchase_messages",
        on_delete=models.CASCADE,
    )
    body = models.CharField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.sender}: {self.body[:60]}"


class DailySiteVisit(models.Model):
    date = models.DateField(db_index=True)
    visitor_hash = models.CharField(max_length=64, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="daily_site_visits",
        on_delete=models.SET_NULL,
    )
    first_path = models.CharField(max_length=500, blank=True)
    last_path = models.CharField(max_length=500, blank=True)
    pageviews = models.PositiveIntegerField(default=1)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["date", "visitor_hash"],
                name="unique_daily_site_visitor",
            ),
        ]
        ordering = ["-date", "-last_seen_at"]

    def __str__(self):
        return f"{self.date}: {self.visitor_hash[:10]} ({self.pageviews})"


class SearchQueryLog(models.Model):
    class Source(models.TextChoices):
        SEARCH = "search", "Поиск"
        CATALOG = "catalog", "Каталог"

    query = models.CharField("Запрос", max_length=500)
    normalized_query = models.CharField("Нормализованный запрос", max_length=500, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="search_query_logs",
        on_delete=models.SET_NULL,
    )
    visitor_hash = models.CharField("Посетитель", max_length=64, db_index=True)
    source = models.CharField("Источник", max_length=20, choices=Source.choices, db_index=True)
    language_code = models.CharField("Язык", max_length=10, blank=True)
    results_count = models.PositiveIntegerField("Найдено результатов", default=0)
    candidates_count = models.PositiveIntegerField("Проверено кандидатов", default=0)
    has_results = models.BooleanField("Есть результаты", default=False, db_index=True)
    searched_at = models.DateTimeField("Время поиска", auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-searched_at", "-id"]
        verbose_name = "поисковый запрос"
        verbose_name_plural = "история поисковых запросов"

    def __str__(self):
        return self.query


class StoreClick(models.Model):
    shop = models.ForeignKey(
        "catalog.Shop",
        null=True,
        related_name="store_clicks",
        on_delete=models.SET_NULL,
    )
    offer = models.ForeignKey(
        "catalog.ProductOffer",
        null=True,
        blank=True,
        related_name="store_clicks",
        on_delete=models.SET_NULL,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="store_clicks",
        on_delete=models.SET_NULL,
    )
    visitor_hash = models.CharField(max_length=64, db_index=True)
    source_path = models.CharField(max_length=500, blank=True)
    clicked_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-clicked_at"]

    def __str__(self):
        return f"{self.shop or 'Unknown shop'} at {self.clicked_at:%Y-%m-%d %H:%M}"


class ShoppingListEvent(models.Model):
    class EventType(models.TextChoices):
        ADDED = "added", "Добавлено"
        REMOVED = "removed", "Удалено"
        REPLACED = "replaced", "Заменено"
        PURCHASED = "purchased", "Куплено"
        UNPURCHASED = "unpurchased", "Снята отметка"
        CLEARED = "cleared", "Список очищен"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="shopping_list_events",
        on_delete=models.SET_NULL,
    )
    shop = models.ForeignKey(
        "catalog.Shop",
        null=True,
        blank=True,
        related_name="shopping_list_events",
        on_delete=models.SET_NULL,
    )
    offer = models.ForeignKey(
        "catalog.ProductOffer",
        null=True,
        blank=True,
        related_name="shopping_list_events",
        on_delete=models.SET_NULL,
    )
    event_type = models.CharField(max_length=20, choices=EventType.choices, db_index=True)
    item_name = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_event_type_display()}: {self.item_name}"
