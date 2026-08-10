import uuid

from django.db import migrations, models


def populate_share_tokens(apps, schema_editor):
    ShoppingList = apps.get_model("main", "ShoppingList")
    for shopping_list in ShoppingList.objects.filter(share_token__isnull=True).iterator():
        shopping_list.share_token = uuid.uuid4()
        shopping_list.save(update_fields=["share_token"])


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0002_shoppinglistitem_is_purchased"),
    ]

    operations = [
        migrations.AddField(
            model_name="shoppinglist",
            name="share_token",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(populate_share_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="shoppinglist",
            name="share_token",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
