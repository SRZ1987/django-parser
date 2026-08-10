from pathlib import Path

from parsers.standalone import catalog_sitemap_retailers_parser

from .base import ParserAdapter, ParserResult
from .utils import count_excel_rows


class CatalogSitemapRetailerAdapter(ParserAdapter):
    worksheet_name = catalog_sitemap_retailers_parser.WORKSHEET_NAME
    column_map = {
        catalog_sitemap_retailers_parser.COLUMNS[0]: "original_name",
        catalog_sitemap_retailers_parser.COLUMNS[1]: "price",
        catalog_sitemap_retailers_parser.COLUMNS[2]: "sale_price",
        catalog_sitemap_retailers_parser.COLUMNS[4]: "barcode",
        catalog_sitemap_retailers_parser.COLUMNS[5]: "external_id",
        catalog_sitemap_retailers_parser.COLUMNS[6]: "image_url",
        catalog_sitemap_retailers_parser.COLUMNS[7]: "product_url",
        catalog_sitemap_retailers_parser.COLUMNS[8]: "sku",
        catalog_sitemap_retailers_parser.COLUMNS[9]: "category_name",
        catalog_sitemap_retailers_parser.COLUMNS[10]: "category_external_id",
        catalog_sitemap_retailers_parser.COLUMNS[11]: "description",
    }

    async def run(self, output_path, log_callback=None):
        output_path = Path(output_path)
        try:
            await catalog_sitemap_retailers_parser.main(
                self.code,
                output_path=output_path,
                log_callback=log_callback,
            )
            return ParserResult(
                success=True,
                output_path=str(output_path),
                products_count=count_excel_rows(output_path, worksheet_name=self.worksheet_name),
            )
        except Exception as exc:
            return ParserResult(
                success=False,
                output_path=str(output_path),
                products_count=0,
                error_message=str(exc),
            )


class ToruJyriAdapter(CatalogSitemapRetailerAdapter):
    code = "torujyri"
    name = "Toru-Jüri"


class ArcadeAdapter(CatalogSitemapRetailerAdapter):
    code = "arcade"
    name = "Arcade"


CATALOG_SITEMAP_RETAILER_ADAPTERS = (ToruJyriAdapter, ArcadeAdapter)
