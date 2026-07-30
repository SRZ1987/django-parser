from pathlib import Path

from parsers.standalone import bauhof_parser

from .base import ParserAdapter, ParserResult
from .utils import count_excel_rows


class BauhofAdapter(ParserAdapter):
    code = "bauhof"
    name = "Bauhof"
    worksheet_name = "Товары"
    column_map = {
        bauhof_parser.COLUMNS[0]: "original_name",
        bauhof_parser.COLUMNS[1]: "price",
        bauhof_parser.COLUMNS[2]: "sale_price",
        bauhof_parser.COLUMNS[4]: "barcode",
        bauhof_parser.COLUMNS[5]: "external_id",
        bauhof_parser.COLUMNS[6]: "image_url",
        bauhof_parser.COLUMNS[7]: "product_url",
    }

    async def run(self, output_path, log_callback=None):
        output_path = Path(output_path)
        try:
            await bauhof_parser.main(output_path=output_path, log_callback=log_callback)
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
