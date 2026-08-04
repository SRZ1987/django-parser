from pathlib import Path

from parsers.standalone import lemona_parser

from .base import ParserAdapter, ParserResult
from .utils import count_excel_rows


class LemonaAdapter(ParserAdapter):
    code = "lemona"
    name = "Lemona"
    worksheet_name = lemona_parser.WORKSHEET_NAME
    column_map = {
        lemona_parser.COLUMNS[0]: "original_name",
        lemona_parser.COLUMNS[1]: "price",
        lemona_parser.COLUMNS[2]: "sale_price",
        lemona_parser.COLUMNS[3]: "quantity_price",
        lemona_parser.COLUMNS[4]: "barcode",
        lemona_parser.COLUMNS[5]: "external_id",
        lemona_parser.COLUMNS[6]: "image_url",
        lemona_parser.COLUMNS[7]: "product_url",
        lemona_parser.COLUMNS[8]: "sku",
        lemona_parser.COLUMNS[9]: "category_name",
        lemona_parser.COLUMNS[10]: "category_external_id",
        lemona_parser.COLUMNS[11]: "description",
        lemona_parser.COLUMNS[12]: "quantity_price_min_quantity",
    }

    async def run(self, output_path, log_callback=None):
        output_path = Path(output_path)
        try:
            await lemona_parser.main(output_path, log_callback=log_callback)
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
