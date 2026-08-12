from django.db import migrations


INDEX_NAMES = (
    "catalog_offer_active_search_trgm",
    "catalog_offer_active_name_trgm",
    "catalog_offer_external_id_trgm",
    "catalog_product_name_trgm",
    "catalog_offer_active_order_idx",
)


def create_postgresql_search_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return

    schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    schema_editor.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS catalog_offer_active_search_trgm
        ON catalog_productoffer USING gin (search_text gin_trgm_ops)
        WHERE is_active AND is_available
        """
    )
    schema_editor.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS catalog_offer_active_name_trgm
        ON catalog_productoffer USING gin (normalized_name gin_trgm_ops)
        WHERE is_active AND is_available
        """
    )
    schema_editor.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS catalog_offer_external_id_trgm
        ON catalog_productoffer USING gin (external_id gin_trgm_ops)
        """
    )
    schema_editor.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS catalog_product_name_trgm
        ON catalog_product USING gin (normalized_name gin_trgm_ops)
        """
    )
    schema_editor.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS catalog_offer_active_order_idx
        ON catalog_productoffer (original_name, id)
        WHERE is_active AND is_available
        """
    )


def remove_postgresql_search_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for index_name in INDEX_NAMES:
        schema_editor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("catalog", "0004_fix_decora_image_urls"),
    ]

    operations = [
        migrations.RunPython(
            create_postgresql_search_indexes,
            remove_postgresql_search_indexes,
        ),
    ]
