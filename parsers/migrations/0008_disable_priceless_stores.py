from django.db import migrations


STORE_CODES = ("bestor", "katusemaailm")


def disable_priceless_stores(apps, schema_editor):
    ParserConfig = apps.get_model("parsers", "ParserConfig")
    ProductOffer = apps.get_model("catalog", "ProductOffer")
    Shop = apps.get_model("catalog", "Shop")

    ParserConfig.objects.filter(code__in=STORE_CODES).update(is_enabled=False)
    ProductOffer.objects.filter(shop__code__in=STORE_CODES).update(
        is_active=False,
        is_available=False,
    )
    Shop.objects.filter(code__in=STORE_CODES).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0004_fix_decora_image_urls"),
        ("parsers", "0007_parserconfig_runtime_settings"),
    ]

    operations = [
        migrations.RunPython(disable_priceless_stores, migrations.RunPython.noop),
    ]
