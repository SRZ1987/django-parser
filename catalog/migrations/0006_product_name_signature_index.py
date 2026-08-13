from django.db import migrations


FUNCTION_NAME = "catalog_product_name_signature"
INDEX_NAME = "catalog_offer_active_name_signature_idx"


def create_name_signature_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return

    schema_editor.execute(
        f"""
        CREATE OR REPLACE FUNCTION {FUNCTION_NAME}(input_name text)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        AS $$
            SELECT md5(COALESCE(string_agg(token, ' ' ORDER BY token), ''))
            FROM unnest(string_to_array(COALESCE(input_name, ''), ' ')) AS parts(token)
            WHERE token <> ''
        $$
        """
    )
    schema_editor.execute(
        f"""
        CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME}
        ON catalog_productoffer ({FUNCTION_NAME}(normalized_name), shop_id)
        WHERE is_active AND is_available
          AND normalized_name <> ''
          AND (price IS NOT NULL OR sale_price IS NOT NULL)
        """
    )


def remove_name_signature_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
    schema_editor.execute(f"DROP FUNCTION IF EXISTS {FUNCTION_NAME}(text)")


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("catalog", "0005_add_postgresql_search_indexes"),
    ]

    operations = [
        migrations.RunPython(
            create_name_signature_index,
            remove_name_signature_index,
        ),
    ]
