from django.core.management.base import BaseCommand

from catalog.models import Shop
from parsers.models import ParserConfig
from parsers.services.espak_client import ESPAK_WEBSITE_URL


class Command(BaseCommand):
    help = "Create or update parser shop and configuration records."

    def handle(self, *args, **options):
        shop, shop_created = Shop.objects.update_or_create(
            code="espak",
            defaults={
                "name": "ESPAK",
                "website_url": ESPAK_WEBSITE_URL,
                "is_active": True,
            },
        )
        parser_config, config_created = ParserConfig.objects.update_or_create(
            code="espak",
            defaults={
                "shop": shop,
                "name": "ESPAK parser",
                "is_enabled": True,
            },
        )

        self.stdout.write(
            self.style.SUCCESS(
                "ESPAK setup complete: shop={shop_status}, parser_config={config_status}, "
                "shop_id={shop_id}, parser_config_id={config_id}".format(
                    shop_status="created" if shop_created else "updated",
                    config_status="created" if config_created else "updated",
                    shop_id=shop.pk,
                    config_id=parser_config.pk,
                )
            )
        )
