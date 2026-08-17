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
from parsers.standalone.feb_parser import BASE_URL as FEB_WEBSITE_URL
from parsers.standalone.lemona_parser import BASE_URL as LEMONA_WEBSITE_URL
from parsers.standalone.motonet_parser import BASE_URL as MOTONET_WEBSITE_URL
from parsers.standalone.oomipood_parser import BASE_URL as OOMIPOOD_WEBSITE_URL
from parsers.standalone.catalog_api_retailers_parser import API_RETAILERS
from parsers.standalone.catalog_listing_retailers_parser import LISTING_RETAILERS
from parsers.standalone.catalog_sitemap_retailers_parser import CATALOG_SITEMAP_RETAILERS
from parsers.standalone.public_commerce_parser import PUBLIC_COMMERCE_STORES
from parsers.standalone.sitemap_retailers_parser import SITEMAP_RETAILERS


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
        for run_order, store in enumerate(PUBLIC_COMMERCE_STORES.values(), start=8):
            self._setup_parser(
                shop_code=store.code,
                shop_name=store.name,
                website_url=store.base_url,
                parser_name=f"{store.name} parser",
                run_order=run_order,
                is_enabled=store.enabled_by_default,
                shop_is_active=store.enabled_by_default,
            )
        next_run_order = 8 + len(PUBLIC_COMMERCE_STORES)
        self._setup_parser(
            shop_code="oomipood",
            shop_name="Oomipood",
            website_url=OOMIPOOD_WEBSITE_URL,
            parser_name="Oomipood parser",
            run_order=next_run_order,
        )
        self._setup_parser(
            shop_code="lemona",
            shop_name="Lemona",
            website_url=LEMONA_WEBSITE_URL,
            parser_name="Lemona parser",
            run_order=next_run_order + 1,
        )
        for offset, store in enumerate(SITEMAP_RETAILERS.values(), start=2):
            self._setup_parser(
                shop_code=store.code,
                shop_name=store.name,
                website_url=store.base_url,
                parser_name=f"{store.name} parser",
                run_order=next_run_order + offset,
            )
        self._setup_parser(
            shop_code="motonet",
            shop_name="Motonet",
            website_url=MOTONET_WEBSITE_URL,
            parser_name="Motonet parser",
            run_order=next_run_order + 2 + len(SITEMAP_RETAILERS),
        )
        additional_retailers = [
            LISTING_RETAILERS["hammerjack"],
            API_RETAILERS["stokker"],
            CATALOG_SITEMAP_RETAILERS["torujyri"],
            API_RETAILERS["esvika"],
            CATALOG_SITEMAP_RETAILERS["arcade"],
            LISTING_RETAILERS["elektrikaup"],
        ]
        first_additional_order = next_run_order + 3 + len(SITEMAP_RETAILERS)
        for run_order, store in enumerate(additional_retailers, start=first_additional_order):
            self._setup_parser(
                shop_code=store.code,
                shop_name=store.name,
                website_url=store.base_url,
                parser_name=f"{store.name} parser",
                run_order=run_order,
            )
        self._setup_parser(
            shop_code="feb",
            shop_name="FEB",
            website_url=FEB_WEBSITE_URL,
            parser_name="FEB parser",
            run_order=first_additional_order + len(additional_retailers),
        )

    def _setup_parser(
        self,
        *,
        shop_code,
        shop_name,
        website_url,
        parser_name,
        run_order,
        is_enabled=True,
        shop_is_active=True,
    ):
        shop, shop_created = Shop.objects.update_or_create(
            code=shop_code,
            defaults={
                "name": shop_name,
                "website_url": website_url,
                "is_active": shop_is_active,
            },
        )
        parser_config, config_created = ParserConfig.objects.update_or_create(
            code=shop_code,
            defaults={
                "shop": shop,
                "name": parser_name,
                "is_enabled": is_enabled,
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
