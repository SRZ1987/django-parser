from .base import BaseStoreParser, ParserError


class FereParser(BaseStoreParser):
    code = "fere"

    def run(self):
        raise ParserError("Parser implementation is not connected yet.")
