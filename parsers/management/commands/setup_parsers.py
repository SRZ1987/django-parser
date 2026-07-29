from django.core.management.base import BaseCommand

from catalog.models import Shop
from parsers.models import ParserConfig
from parsers.services.depo_client import DEPO_WEBSITE_URL
from parsers.services.espak_client import ESPAK_WEBSITE_URL


class Command(BaseCommand):
    help = "Create or update parser shop and configuration records."

    def handle(self, *args, **options):
        self._setup_parser(
            shop_code="depo",
            shop_name="DEPO",
            website_url=DEPO_WEBSITE_URL,
            parser_name="DEPO parser",
        )
        self._setup_parser(
            shop_code="espak",
            shop_name="ESPAK",
            website_url=ESPAK_WEBSITE_URL,
            parser_name="ESPAK parser",
        )

    def _setup_parser(self, *, shop_code, shop_name, website_url, parser_name):
        shop, shop_created = Shop.objects.update_or_create(
            code=shop_code,
            defaults={
                "name": shop_name,
                "website_url": website_url,
                "is_active": True,
            },
        )
        parser_config, config_created = ParserConfig.objects.update_or_create(
            code=shop_code,
            defaults={
                "shop": shop,
                "name": parser_name,
                "is_enabled": True,
            },
        )

        self.stdout.write(
            self.style.SUCCESS(
                "{code} setup complete: shop={shop_status}, parser_config={config_status}, "
                "shop_id={shop_id}, parser_config_id={config_id}".format(
                    code=shop_code.upper(),
                    shop_status="created" if shop_created else "updated",
                    config_status="created" if config_created else "updated",
                    shop_id=shop.pk,
                    config_id=parser_config.pk,
                )
            )
        )
