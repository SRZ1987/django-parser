from pathlib import Path

from parsers.standalone import fere_parser

from .base import ParserAdapter, ParserResult
from .espak import count_excel_rows


class FereAdapter(ParserAdapter):
    code = "fere"
    name = "FERE"
    worksheet_name = None
    column_map = {
        fere_parser.COLUMNS[0]: "original_name",
        fere_parser.COLUMNS[1]: "price",
        fere_parser.COLUMNS[2]: "sale_price",
        fere_parser.COLUMNS[4]: "barcode",
        fere_parser.COLUMNS[5]: "external_id",
        fere_parser.COLUMNS[6]: "image_url",
        fere_parser.COLUMNS[7]: "product_url",
    }

    async def run(self, output_path, log_callback=None):
        output_path = Path(output_path)
        try:
            await fere_parser.main(output_path=output_path, log_callback=log_callback)
            return ParserResult(
                success=True,
                output_path=str(output_path),
                products_count=count_excel_rows(output_path),
            )
        except Exception as exc:
            return ParserResult(
                success=False,
                output_path=str(output_path),
                products_count=0,
                error_message=str(exc),
            )
