from .base import BaseStoreParser, ParserError


class EhituseABCParser(BaseStoreParser):
    code = "ehituseabc"

    def run(self):
        raise ParserError("Parser implementation is not connected yet.")
