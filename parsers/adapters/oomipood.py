from pathlib import Path

from parsers.standalone import oomipood_parser

from .base import ParserAdapter, ParserResult
from .utils import count_excel_rows


class OomipoodAdapter(ParserAdapter):
    code = "oomipood"
    name = "Oomipood"
    worksheet_name = oomipood_parser.WORKSHEET_NAME
    column_map = {
        oomipood_parser.COLUMNS[0]: "original_name",
        oomipood_parser.COLUMNS[1]: "price",
        oomipood_parser.COLUMNS[2]: "sale_price",
        oomipood_parser.COLUMNS[4]: "barcode",
        oomipood_parser.COLUMNS[5]: "external_id",
        oomipood_parser.COLUMNS[6]: "image_url",
        oomipood_parser.COLUMNS[7]: "product_url",
        oomipood_parser.COLUMNS[8]: "sku",
        oomipood_parser.COLUMNS[9]: "category_name",
        oomipood_parser.COLUMNS[10]: "category_external_id",
        oomipood_parser.COLUMNS[11]: "description",
    }

    async def run(self, output_path, log_callback=None):
        output_path = Path(output_path)
        try:
            await oomipood_parser.main(output_path, log_callback=log_callback)
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
