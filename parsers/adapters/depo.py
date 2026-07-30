from pathlib import Path

from parsers.standalone import depo_parser

from .base import ParserAdapter, ParserResult
from .utils import count_excel_rows


class DepoAdapter(ParserAdapter):
    code = "depo"
    name = "DEPO"
    worksheet_name = "Товары"
    column_map = {
        depo_parser.COLUMNS[0]: "original_name",
        depo_parser.COLUMNS[1]: "price",
        depo_parser.COLUMNS[2]: "sale_price",
        depo_parser.COLUMNS[4]: "barcode",
        depo_parser.COLUMNS[5]: "external_id",
        depo_parser.COLUMNS[6]: "image_url",
        depo_parser.COLUMNS[7]: "product_url",
    }

    async def run(self, output_path, log_callback=None):
        output_path = Path(output_path)
        try:
            await depo_parser.main(output_path=output_path, log_callback=log_callback)
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
