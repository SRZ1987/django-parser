from django.core.management.base import BaseCommand

from catalog.models import Shop
from parsers.models import ParserConfig
from parsers.services.bauhaus import BAUHAUS_WEBSITE_URL
from parsers.services.bauhof import BAUHOF_WEBSITE_URL
from parsers.services.depo_client import DEPO_WEBSITE_URL
from parsers.services.ehituseabc import EHITUSEABC_WEBSITE_URL
from parsers.services.espak_client import ESPAK_WEBSITE_URL
from parsers.services.fere import FERE_WEBSITE_URL
from parsers.standalone.handymann_parser import BASE_URL as HANDYMANN_WEBSITE_URL


class Command(BaseCommand):
    help = "Create or update parser shop and configuration records."

    def handle(self, *args, **options):
        self._setup_parser(
            shop_code="depo",
            shop_name="DEPO",
            website_url=DEPO_WEBSITE_URL,
            parser_name="DEPO parser",
            run_order=2,
        )
        self._setup_parser(
            shop_code="espak",
            shop_name="ESPAK",
            website_url=ESPAK_WEBSITE_URL,
            parser_name="ESPAK parser",
            run_order=1,
        )
        self._setup_parser(
            shop_code="bauhaus",
            shop_name="BAUHAUS",
            website_url=BAUHAUS_WEBSITE_URL,
            parser_name="BAUHAUS parser",
            run_order=6,
        )
        self._setup_parser(
            shop_code="bauhof",
            shop_name="Bauhof",
            website_url=BAUHOF_WEBSITE_URL,
            parser_name="Bauhof parser",
            run_order=3,
        )
        self._setup_parser(
            shop_code="ehituseabc",
            shop_name="Ehituse ABC",
            website_url=EHITUSEABC_WEBSITE_URL,
            parser_name="Ehituse ABC parser",
            run_order=4,
        )
        self._setup_parser(
            shop_code="fere",
            shop_name="FERE",
            website_url=FERE_WEBSITE_URL,
            parser_name="FERE parser",
            run_order=5,
        )
        self._setup_parser(
            shop_code="handymann",
            shop_name="Handymann",
            website_url=HANDYMANN_WEBSITE_URL,
            parser_name="Handymann parser",
            run_order=7,
        )

    def _setup_parser(self, *, shop_code, shop_name, website_url, parser_name, run_order):
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
                "run_order": run_order,
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
