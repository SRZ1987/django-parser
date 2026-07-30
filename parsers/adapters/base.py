from dataclasses import dataclass


@dataclass
class ParserResult:
    success: bool
    output_path: str
    products_count: int
    error_message: str = ""


class ParserAdapter:
    code = ""
    name = ""
    column_map = {}
    worksheet_name = None

    async def run(self, output_path, log_callback=None):
        raise NotImplementedError
