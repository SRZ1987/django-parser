from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("parsers", "0006_parserrun_unique_running_parser_run_per_parser"),
    ]

    operations = [
        migrations.AddField(
            model_name="parserconfig",
            name="runtime_settings",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
