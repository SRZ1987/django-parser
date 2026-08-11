from django.db import migrations
from django.utils import timezone


def seed_group_purchases(apps, schema_editor):
    ShoppingListItem = apps.get_model("main", "ShoppingListItem")
    GroupPurchase = apps.get_model("main", "GroupPurchase")
    GroupPurchaseMember = apps.get_model("main", "GroupPurchaseMember")
    now = timezone.now()

    items = ShoppingListItem.objects.select_related(
        "shopping_list",
        "source_offer",
    ).iterator(chunk_size=500)
    for item in items:
        offer = item.source_offer
        current_price = offer.sale_price if offer.sale_price is not None else offer.price
        if not (
            offer.is_active
            and offer.is_available
            and current_price is not None
            and offer.quantity_price is not None
            and offer.quantity_price_min_quantity is not None
            and offer.quantity_price_min_quantity >= 2
            and offer.quantity_price < current_price
        ):
            continue

        group, _created = GroupPurchase.objects.get_or_create(
            offer_id=offer.pk,
            status="open",
            defaults={
                "target_quantity": offer.quantity_price_min_quantity,
                "quantity_price": offer.quantity_price,
                "last_activity_at": now,
            },
        )
        GroupPurchaseMember.objects.get_or_create(
            group_id=group.pk,
            user_id=item.shopping_list.user_id,
            defaults={"shopping_list_item_id": item.pk},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("main", "0006_group_purchases"),
    ]

    operations = [
        migrations.RunPython(seed_group_purchases, migrations.RunPython.noop),
    ]
