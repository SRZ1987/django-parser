from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0002_productoffer_barcode_checked_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="productoffer",
            name="quantity_price",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="productoffer",
            name="quantity_price_min_quantity",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pricehistory",
            name="quantity_price",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="pricehistory",
            name="quantity_price_min_quantity",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
