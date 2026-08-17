from pathlib import Path

from parsers.standalone import feb_parser

from .base import ParserAdapter, ParserResult
from .utils import count_excel_rows


class FebAdapter(ParserAdapter):
    code = "feb"
    name = "FEB"
    worksheet_name = feb_parser.WORKSHEET_NAME
    column_map = {
        feb_parser.COLUMNS[0]: "original_name",
        feb_parser.COLUMNS[1]: "price",
        feb_parser.COLUMNS[2]: "sale_price",
        feb_parser.COLUMNS[4]: "barcode",
        feb_parser.COLUMNS[5]: "external_id",
        feb_parser.COLUMNS[6]: "image_url",
        feb_parser.COLUMNS[7]: "product_url",
        feb_parser.COLUMNS[8]: "sku",
        feb_parser.COLUMNS[9]: "category_name",
        feb_parser.COLUMNS[10]: "category_external_id",
        feb_parser.COLUMNS[11]: "description",
        feb_parser.COLUMNS[12]: "brand",
        feb_parser.COLUMNS[13]: "model",
    }

    async def run(self, output_path, log_callback=None):
        output_path = Path(output_path)
        try:
            await feb_parser.main(output_path=output_path, log_callback=log_callback)
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
