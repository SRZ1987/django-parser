from decimal import Decimal

from django.test import TestCase

from catalog.models import Category, Product, ProductOffer, Shop
from catalog.services.attribute_extraction import extract_product_attributes
from catalog.services.normalization import normalize_product_name
from catalog.services.product_matching import (
    MATCH_BUNDLE_OR_VARIANT,
    MATCH_EXACT,
    MATCH_SAME_PRODUCT,
    MATCH_SIMILAR_PRODUCT,
)
from catalog.services.product_search import search_products


class ProductSearchTests(TestCase):
    def setUp(self):
        self.espak = Shop.objects.create(name="ESPAK", code="espak")
        self.depo = Shop.objects.create(name="DEPO", code="depo")
        self.bauhof = Shop.objects.create(name="Bauhof", code="bauhof")
        self.tools = Category.objects.create(shop=self.espak, external_id="tools", name="Tools")
        self.fasteners = Category.objects.create(shop=self.espak, external_id="fasteners", name="Fasteners")

    def offer(
        self,
        name,
        *,
        shop=None,
        category=None,
        sku=None,
        barcode="",
        brand="",
        model="",
        price="10.00",
        is_active=True,
        is_available=True,
    ):
        shop = shop or self.espak
        category = category or self.tools
        sku = sku or f"SKU-{ProductOffer.objects.count() + 1}"
        product = Product.objects.create(name=name, brand=brand, model=model, barcode=barcode)
        return ProductOffer.objects.create(
            shop=shop,
            product=product,
            category=category,
            external_id=sku,
            sku=sku,
            barcode=barcode,
            original_name=name,
            price=Decimal(price),
            currency="EUR",
            is_active=is_active,
            is_available=is_available,
        )

    def ids(self, matches):
        return [match.offer.pk for match in matches]

    def test_exact_barcode_returns_all_barcode_matches_first(self):
        espak = self.offer("Makita DDF482Z drill", barcode="4000000000001", brand="Makita", model="DDF482Z")
        depo = self.offer("Akutrell Makita DDF 482 Z", shop=self.depo, barcode="4000000000001", brand="Makita", model="DDF482Z")
        self.offer("Bosch drill", shop=self.bauhof, barcode="999", brand="Bosch", model="GSR18V")

        results = search_products("4000000000001")

        self.assertEqual(set(self.ids(results.exact_matches)), {espak.pk, depo.pk})
        self.assertTrue(all(match.match_type == MATCH_EXACT for match in results.exact_matches))
        self.assertEqual(results.price_summary.offers_count, 2)

    def test_barcode_seed_finds_model_match_without_barcode(self):
        source = self.offer("Makita DDF482Z drill", barcode="4000000000001", brand="Makita", model="DDF482Z")
        analog = self.offer("Akutrell MAKITA DDF 482 Z 18 V", shop=self.depo, barcode="", brand="Makita", model="DDF482Z")

        results = search_products(source.barcode)

        self.assertIn(source.pk, self.ids(results.exact_matches))
        self.assertIn(analog.pk, self.ids(results.same_product))

    def test_exact_shop_code_is_starting_point_for_catalog_matching(self):
        source = self.offer("Makita DDF482Z drill", sku="MAK-DDF482Z", barcode="", brand="Makita", model="DDF482Z")
        other = self.offer("Makita DDF482Z LXT", shop=self.depo, sku="DEPO-42", brand="Makita", model="DDF482Z")

        results = search_products("MAK-DDF482Z")

        self.assertIn(source.pk, self.ids(results.exact_matches))
        self.assertIn(other.pk, self.ids(results.same_product))

    def test_model_matching_handles_split_and_joined_model(self):
        same = self.offer("MAKITA DDF482Z LXT", shop=self.depo, brand="Makita")

        results = search_products("Makita DDF 482")

        self.assertIn(same.pk, self.ids(results.same_product))

    def test_different_brands_are_not_grouped_as_same_exact_product(self):
        self.offer("Makita DDF482Z 18V", brand="Makita", model="DDF482Z")
        bosch = self.offer("Bosch GSR 18V drill", shop=self.depo, brand="Bosch", model="GSR18V")

        results = search_products("Makita DDF482 18V")

        self.assertNotIn(bosch.pk, self.ids(results.exact_matches))
        self.assertNotIn(bosch.pk, self.ids(results.same_product))

    def test_same_fastener_size_ranks_above_other_size(self):
        same_size = self.offer("Screw 5x70mm 100 pcs", category=self.fasteners, sku="SAME", price="5.00")
        other_size = self.offer("Screw 5x90mm 100 pcs", category=self.fasteners, sku="OTHER", price="6.00")

        results = search_products("screw 5x70")
        ranked_ids = self.ids(results.similar_products + results.same_product + results.bundles_or_variants)

        self.assertLess(ranked_ids.index(same_size.pk), ranked_ids.index(other_size.pk))

    def test_package_quantity_influences_ranking(self):
        pack_100 = self.offer("Screw 5x70mm 100 pcs", category=self.fasteners, sku="PACK100")
        pack_1000 = self.offer("Screw 5x70mm 1000 pcs", category=self.fasteners, sku="PACK1000")

        results = search_products("screw 5x70 100 pcs")
        ranked_ids = self.ids(results.similar_products + results.same_product + results.bundles_or_variants)

        self.assertLess(ranked_ids.index(pack_100.pk), ranked_ids.index(pack_1000.pk))

    def test_bundle_is_not_exact_bare_tool_but_is_shown_nearby(self):
        bare = self.offer("Makita DDF482Z bare tool", brand="Makita", model="DDF482Z")
        kit = self.offer("Makita DDF482RTJ 2x5Ah charger case", shop=self.depo, brand="Makita", model="DDF482RTJ")

        results = search_products("Makita DDF482")

        self.assertIn(bare.pk, self.ids(results.same_product + results.exact_matches))
        self.assertIn(kit.pk, self.ids(results.bundles_or_variants))
        self.assertNotIn(kit.pk, self.ids(results.exact_matches))

    def test_plain_name_query_returns_relevant_set(self):
        drill = self.offer("Bosch drill GSR 18V", brand="Bosch", model="GSR18V")
        saw = self.offer("Makita circular saw", shop=self.depo, brand="Makita")

        results = search_products("bosch drill")

        self.assertIn(drill.pk, self.ids(results.similar_products + results.same_product))
        self.assertNotIn(saw.pk, self.ids(results.exact_matches))

    def test_absent_barcode_and_brand_still_search_by_name(self):
        offer = self.offer("Generic screw 5x70mm", barcode="", brand="", model="", category=self.fasteners)

        results = search_products("generic 5x70")

        self.assertIn(offer.pk, self.ids(results.similar_products + results.same_product))

    def test_inactive_and_unavailable_offers_are_hidden(self):
        self.offer("Visible Makita DDF482", brand="Makita", model="DDF482Z")
        inactive = self.offer("Inactive Makita DDF482", sku="INACTIVE", is_active=False, brand="Makita", model="DDF482Z")
        unavailable = self.offer("Unavailable Makita DDF482", sku="UNAVAILABLE", is_available=False, brand="Makita", model="DDF482Z")

        results = search_products("Makita DDF482")
        all_ids = self.ids(results.exact_matches + results.same_product + results.bundles_or_variants + results.similar_products)

        self.assertNotIn(inactive.pk, all_ids)
        self.assertNotIn(unavailable.pk, all_ids)

    def test_sqlite_fallback_limits_candidate_count(self):
        for index in range(20):
            self.offer(f"Bosch drill {index}", sku=f"BOSCH-{index}", brand="Bosch")

        results = search_products("bosch", candidate_limit=5, results_limit=5)

        self.assertLessEqual(results.candidates_count, 5)
        self.assertLessEqual(results.total_count, 5)


class AttributeExtractionTests(TestCase):
    def test_extracts_model_dimensions_weight_and_battery(self):
        attributes = extract_product_attributes("Makita DDF482 18V 2x5Ah screw 5x70mm 1kg")

        self.assertEqual(attributes.brand, "makita")
        self.assertEqual(attributes.model, "ddf482")
        self.assertEqual(attributes.voltage, "18v")
        self.assertEqual(attributes.battery_count, 2)
        self.assertEqual(attributes.battery_capacity, "5ah")
        self.assertEqual(attributes.dimensions, ("5mm", "70mm"))
        self.assertEqual(attributes.weight, "1kg")

    def test_normalization_keeps_model_and_normalizes_units(self):
        self.assertEqual(normalize_product_name("Makita DDF 482 Z 18 V 5×70 мм"), "makita ddf482 z 18v 5x70mm")
