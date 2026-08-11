from pathlib import Path

from parsers.standalone import ehituseabc_parser

from .base import ParserAdapter, ParserResult
from .utils import count_excel_rows


class EhituseABCAdapter(ParserAdapter):
    code = "ehituseabc"
    name = "EhituseABC"
    worksheet_name = "Товары"
    column_map = {
        ehituseabc_parser.COLUMNS[0]: "original_name",
        ehituseabc_parser.COLUMNS[1]: "price",
        ehituseabc_parser.COLUMNS[2]: "sale_price",
        ehituseabc_parser.COLUMNS[4]: "barcode",
        ehituseabc_parser.COLUMNS[5]: "external_id",
        ehituseabc_parser.COLUMNS[6]: "image_url",
        ehituseabc_parser.COLUMNS[7]: "product_url",
        ehituseabc_parser.COLUMNS[8]: "sku",
        ehituseabc_parser.COLUMNS[9]: "category_name",
        ehituseabc_parser.COLUMNS[10]: "category_external_id",
        ehituseabc_parser.COLUMNS[11]: "description",
        ehituseabc_parser.COLUMNS[12]: "brand",
        ehituseabc_parser.COLUMNS[13]: "model",
    }

    async def run(self, output_path, log_callback=None):
        output_path = Path(output_path)
        try:
            await ehituseabc_parser.main(output_path=output_path, log_callback=log_callback)
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
