from django.db import migrations


def add_feb_parser(apps, schema_editor):
    Shop = apps.get_model("catalog", "Shop")
    ParserConfig = apps.get_model("parsers", "ParserConfig")

    shop, _created = Shop.objects.update_or_create(
        code="feb",
        defaults={
            "name": "FEB",
            "website_url": "https://www.feb.ee/",
            "is_active": True,
        },
    )
    ParserConfig.objects.update_or_create(
        code="feb",
        defaults={
            "shop": shop,
            "name": "FEB parser",
            "is_enabled": True,
            "run_order": 40,
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("parsers", "0008_disable_priceless_stores"),
    ]

    operations = [
        migrations.RunPython(add_feb_parser, migrations.RunPython.noop),
    ]
