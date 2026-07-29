from .base import BaseStoreParser, ParserError


class EspakParser(BaseStoreParser):
    code = "espak"

    def run(self):
        raise ParserError("Parser implementation is not connected yet.")
