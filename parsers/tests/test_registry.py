from django.test import SimpleTestCase

from parsers.services.base import ParserError
from parsers.services.depo import DepoParser
from parsers.services.registry import get_parser_class


class ParserRegistryTests(SimpleTestCase):
    def test_get_parser_class_returns_registered_parser(self):
        self.assertIs(get_parser_class("depo"), DepoParser)

    def test_get_parser_class_raises_for_unknown_code(self):
        with self.assertRaisesMessage(ParserError, "Unknown parser code: unknown"):
            get_parser_class("unknown")
