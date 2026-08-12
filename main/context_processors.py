from django.db.models import Count

from .models import ShoppingList


def shopping_list_summary(request):
    if not request.user.is_authenticated:
        return {"shopping_list_item_count": None}

    item_count = (
        ShoppingList.objects.filter(user_id=request.user.pk)
        .order_by()
        .annotate(item_count=Count("items"))
        .values_list("item_count", flat=True)
        .first()
    )
    return {"shopping_list_item_count": item_count}
