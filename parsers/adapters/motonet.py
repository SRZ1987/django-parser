from pathlib import Path

from parsers.standalone import motonet_parser

from .base import ParserAdapter, ParserResult
from .utils import count_excel_rows


class MotonetAdapter(ParserAdapter):
    code = "motonet"
    name = "Motonet"
    worksheet_name = motonet_parser.WORKSHEET_NAME
    column_map = {
        motonet_parser.COLUMNS[0]: "original_name",
        motonet_parser.COLUMNS[1]: "price",
        motonet_parser.COLUMNS[2]: "sale_price",
        motonet_parser.COLUMNS[4]: "barcode",
        motonet_parser.COLUMNS[5]: "external_id",
        motonet_parser.COLUMNS[6]: "image_url",
        motonet_parser.COLUMNS[7]: "product_url",
        motonet_parser.COLUMNS[8]: "sku",
        motonet_parser.COLUMNS[9]: "category_name",
        motonet_parser.COLUMNS[10]: "category_external_id",
        motonet_parser.COLUMNS[11]: "description",
    }

    async def run(self, output_path, log_callback=None):
        output_path = Path(output_path)
        try:
            await motonet_parser.main(output_path=output_path, log_callback=log_callback)
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
