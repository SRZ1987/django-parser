from .base import BaseStoreParser, ParserError


class DepoParser(BaseStoreParser):
    code = "depo"

    def run(self):
        raise ParserError("Parser implementation is not connected yet.")
