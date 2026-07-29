from .base import BaseStoreParser, ParserError


class BauhausParser(BaseStoreParser):
    code = "bauhaus"

    def run(self):
        raise ParserError("Parser implementation is not connected yet.")
