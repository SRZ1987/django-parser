from .base import ParserError
from .bauhaus import BauhausParser
from .bauhof import BauhofParser
from .depo import DepoParser
from .ehituseabc import EhituseABCParser
from .espak import EspakParser
from .fere import FereParser


PARSER_REGISTRY = {
    "depo": DepoParser,
    "bauhaus": BauhausParser,
    "bauhof": BauhofParser,
    "ehituseabc": EhituseABCParser,
    "espak": EspakParser,
    "fere": FereParser,
}


def get_parser_class(code):
    try:
        return PARSER_REGISTRY[code]
    except KeyError as exc:
        raise ParserError(f"Unknown parser code: {code}") from exc
