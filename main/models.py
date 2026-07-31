from django.conf import settings
from django.db import models


class ShoppingList(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        related_name="shopping_list",
        on_delete=models.CASCADE,
    )
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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["shopping_list", "source_offer"],
                name="unique_shopping_list_source_offer",
            ),
        ]
        ordering = ["name", "id"]

    def __str__(self):
        return self.name
