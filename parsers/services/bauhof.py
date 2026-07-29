from .base import BaseStoreParser, ParserError


class BauhofParser(BaseStoreParser):
    code = "bauhof"

    def run(self):
        raise ParserError("Parser implementation is not connected yet.")
