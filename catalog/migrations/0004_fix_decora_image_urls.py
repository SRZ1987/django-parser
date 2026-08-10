from django.db import migrations
from django.db.models import F, Value
from django.db.models.functions import Replace


BROKEN_PATH = "https://www.decora.ee/needtochange/"
VALID_PATH = "https://www.decora.ee/"


def fix_decora_image_urls(apps, schema_editor):
    ProductOffer = apps.get_model("catalog", "ProductOffer")
    ProductOffer.objects.filter(
        shop__code="decora",
        image_url__startswith=BROKEN_PATH,
    ).update(
        image_url=Replace(F("image_url"), Value(BROKEN_PATH), Value(VALID_PATH)),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0003_productoffer_quantity_pricing"),
    ]

    operations = [
        migrations.RunPython(fix_decora_image_urls, migrations.RunPython.noop),
    ]
