from pathlib import Path

from parsers.standalone import public_commerce_parser

from .base import ParserAdapter, ParserResult
from .utils import count_excel_rows


class PublicCommerceAdapter(ParserAdapter):
    worksheet_name = public_commerce_parser.WORKSHEET_NAME
    column_map = {
        public_commerce_parser.COLUMNS[0]: "original_name",
        public_commerce_parser.COLUMNS[1]: "price",
        public_commerce_parser.COLUMNS[2]: "sale_price",
        public_commerce_parser.COLUMNS[4]: "barcode",
        public_commerce_parser.COLUMNS[5]: "external_id",
        public_commerce_parser.COLUMNS[6]: "image_url",
        public_commerce_parser.COLUMNS[7]: "product_url",
        public_commerce_parser.COLUMNS[8]: "sku",
        public_commerce_parser.COLUMNS[9]: "category_name",
        public_commerce_parser.COLUMNS[10]: "category_external_id",
        public_commerce_parser.COLUMNS[11]: "description",
    }

    async def run(self, output_path, log_callback=None):
        output_path = Path(output_path)
        try:
            await public_commerce_parser.main(
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


class EmartAdapter(PublicCommerceAdapter):
    code = "emart"
    name = "Emart"


class NordhauserAdapter(PublicCommerceAdapter):
    code = "nordhauser"
    name = "Nordhauser"


class MakservAdapter(PublicCommerceAdapter):
    code = "makserv"
    name = "Makserv"


class EcopoodAdapter(PublicCommerceAdapter):
    code = "ecopood"
    name = "Ecopood"


class TevokaupAdapter(PublicCommerceAdapter):
    code = "tevokaup"
    name = "Tevo Ehituskaup"


class VannitoapoodAdapter(PublicCommerceAdapter):
    code = "vannitoapood"
    name = "Vannitoapood"


class TetkoAdapter(PublicCommerceAdapter):
    code = "tetko"
    name = "Tetko"


class FastenerestAdapter(PublicCommerceAdapter):
    code = "fastenerest"
    name = "FastenerEst"


class BestorAdapter(PublicCommerceAdapter):
    code = "bestor"
    name = "Bestor"


class TooriistapoodAdapter(PublicCommerceAdapter):
    code = "tooriistapood"
    name = "Tööriistapood"


class Katus24Adapter(PublicCommerceAdapter):
    code = "katus24"
    name = "Katus24"


class EhitusoutletAdapter(PublicCommerceAdapter):
    code = "ehitusoutlet"
    name = "Ehitusoutlet"


class HuttonAdapter(PublicCommerceAdapter):
    code = "hutton"
    name = "Hutton"


class AquelAdapter(PublicCommerceAdapter):
    code = "aquel"
    name = "Aquel"


class EhitaksAdapter(PublicCommerceAdapter):
    code = "ehitaks"
    name = "Ehitaks"


class KatusemaailmAdapter(PublicCommerceAdapter):
    code = "katusemaailm"
    name = "Katusemaailm"


class InterstudioAdapter(PublicCommerceAdapter):
    code = "interstudio"
    name = "Interstudio"


class Plaat24Adapter(PublicCommerceAdapter):
    code = "plaat24"
    name = "Plaat24"


class KatusematerjalAdapter(PublicCommerceAdapter):
    code = "katusematerjal"
    name = "Katusematerjal"


class HordenAdapter(PublicCommerceAdapter):
    code = "horden"
    name = "Horden"


class DecoraAdapter(PublicCommerceAdapter):
    code = "decora"
    name = "Decora"


PUBLIC_COMMERCE_ADAPTERS = (
    EmartAdapter,
    NordhauserAdapter,
    MakservAdapter,
    EcopoodAdapter,
    TevokaupAdapter,
    VannitoapoodAdapter,
    TetkoAdapter,
    FastenerestAdapter,
    BestorAdapter,
    TooriistapoodAdapter,
    Katus24Adapter,
    EhitusoutletAdapter,
    HuttonAdapter,
    AquelAdapter,
    EhitaksAdapter,
    KatusemaailmAdapter,
    InterstudioAdapter,
    Plaat24Adapter,
    KatusematerjalAdapter,
    HordenAdapter,
    DecoraAdapter,
)
