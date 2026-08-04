from pathlib import Path

from parsers.standalone import sitemap_retailers_parser

from .base import ParserAdapter, ParserResult
from .utils import count_excel_rows


class SitemapRetailerAdapter(ParserAdapter):
    worksheet_name = sitemap_retailers_parser.WORKSHEET_NAME
    column_map = {
        sitemap_retailers_parser.COLUMNS[0]: "original_name",
        sitemap_retailers_parser.COLUMNS[1]: "price",
        sitemap_retailers_parser.COLUMNS[2]: "sale_price",
        sitemap_retailers_parser.COLUMNS[4]: "barcode",
        sitemap_retailers_parser.COLUMNS[5]: "external_id",
        sitemap_retailers_parser.COLUMNS[6]: "image_url",
        sitemap_retailers_parser.COLUMNS[7]: "product_url",
        sitemap_retailers_parser.COLUMNS[8]: "sku",
        sitemap_retailers_parser.COLUMNS[9]: "category_name",
        sitemap_retailers_parser.COLUMNS[10]: "category_external_id",
        sitemap_retailers_parser.COLUMNS[11]: "description",
    }

    async def run(self, output_path, log_callback=None):
        output_path = Path(output_path)
        try:
            await sitemap_retailers_parser.main(
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


class VipexAdapter(SitemapRetailerAdapter):
    code = "vipex"
    name = "Vipex"


class EffexAdapter(SitemapRetailerAdapter):
    code = "effex"
    name = "Effex"
