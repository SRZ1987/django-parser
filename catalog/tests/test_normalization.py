from django.test import SimpleTestCase

from catalog.services.normalization import (
    build_search_text,
    normalize_brand,
    normalize_model,
    normalize_product_name,
    normalize_text,
)


class NormalizationTests(SimpleTestCase):
    def test_normalizes_case_and_yo(self):
        self.assertEqual(normalize_text("ШУРУПОВЁРТ Bosch"), "шуруповерт bosch")

    def test_normalizes_decimal_comma_and_multiply_sign(self):
        self.assertEqual(normalize_text("Аккумулятор 2,0 Ah × 2"), "аккумулятор 2.0 ah x 2")

    def test_preserves_model_numbers_and_units(self):
        self.assertEqual(
            normalize_product_name("Bosch GSR 18V-50 2.0Ah 125mm 10kg 500W 50x100"),
            "bosch gsr 18v 50 2.0ah 125mm 10kg 500w 50x100",
        )

    def test_handles_empty_values(self):
        self.assertEqual(normalize_text(None), "")
        self.assertEqual(normalize_brand(""), "")
        self.assertEqual(normalize_model("   "), "")

    def test_build_search_text_skips_empty_values(self):
        self.assertEqual(build_search_text("Bosch", "", None, "GSR 18V-50"), "bosch gsr 18v 50")
