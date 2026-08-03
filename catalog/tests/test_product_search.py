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
        sale_price=None,
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
            price=Decimal(price) if price is not None else None,
            sale_price=Decimal(sale_price) if sale_price is not None else None,
            currency="EUR",
            is_active=is_active,
            is_available=is_available,
        )

    def ids(self, matches):
        return [match.offer.pk for match in matches]

    def result_ids(self, results):
        return self.ids(results.matches)

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
        ranked_ids = self.result_ids(results)

        self.assertLess(ranked_ids.index(same_size.pk), ranked_ids.index(other_size.pk))

    def test_package_quantity_influences_ranking(self):
        pack_100 = self.offer("Screw 5x70mm 100 pcs", category=self.fasteners, sku="PACK100")
        pack_1000 = self.offer("Screw 5x70mm 1000 pcs", category=self.fasteners, sku="PACK1000")

        results = search_products("screw 5x70 100 pcs")
        ranked_ids = self.result_ids(results)

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

    def test_text_search_is_order_independent_for_brand_and_name(self):
        target = self.offer("Akutrell MAKITA DDF482Z 18V", shop=self.depo, brand="Makita", model="DDF482Z")
        makita_partial = self.offer("Makita saag", shop=self.bauhof, brand="Makita")
        self.offer("Trell Bosch GSR 18V", brand="Bosch", model="GSR18V")

        direct = search_products("trell makita")
        reversed_query = search_products("makita trell")
        direct_ids = self.result_ids(direct)
        reversed_ids = self.result_ids(reversed_query)

        self.assertIn(target.pk, direct_ids)
        self.assertIn(target.pk, reversed_ids)
        self.assertEqual(direct_ids[:1], [target.pk])
        self.assertEqual(reversed_ids[:1], [target.pk])
        self.assertLess(reversed_ids.index(target.pk), reversed_ids.index(makita_partial.pk))

    def test_text_search_is_order_independent_for_model_and_brand(self):
        target = self.offer("Akutrell Makita DDF482Z", shop=self.depo, brand="Makita", model="DDF482Z")

        direct = search_products("ddf482 makita")
        reversed_query = search_products("makita ddf482")

        self.assertIn(target.pk, self.result_ids(direct))
        self.assertIn(target.pk, self.result_ids(reversed_query))
        self.assertEqual(self.result_ids(direct)[:1], self.result_ids(reversed_query)[:1])

    def test_text_search_is_order_independent_for_dimensions_and_name(self):
        target = self.offer("Kruvi 5x70mm 100 pcs", category=self.fasteners, sku="KRUVI-5X70")
        other_size = self.offer("Kruvi 5x90mm 100 pcs", category=self.fasteners, sku="KRUVI-5X90")

        direct = search_products("5x70 kruvi")
        reversed_query = search_products("kruvi 5x70")
        direct_ids = self.result_ids(direct)
        reversed_ids = self.result_ids(reversed_query)

        self.assertIn(target.pk, direct_ids)
        self.assertIn(target.pk, reversed_ids)
        self.assertLess(direct_ids.index(target.pk), direct_ids.index(other_size.pk))
        self.assertLess(reversed_ids.index(target.pk), reversed_ids.index(other_size.pk))

    def test_absent_barcode_and_brand_still_search_by_name(self):
        offer = self.offer("Generic screw 5x70mm", barcode="", brand="", model="", category=self.fasteners)

        results = search_products("generic 5x70")

        self.assertIn(offer.pk, self.ids(results.similar_products + results.same_product))

    def test_compound_word_and_number_match_ehitusnael(self):
        target = self.offer("Ehitusnael 3,1x100 mm", category=self.fasteners, sku="NAEL-100")
        wrong_length = self.offer("Ehitusnael 3,1x1000 mm", category=self.fasteners, sku="NAEL-1000")

        results = search_products("nael 100")
        result_ids = self.result_ids(results)

        self.assertIn(target.pk, result_ids)
        self.assertLess(result_ids.index(target.pk), result_ids.index(wrong_length.pk))

    def test_compound_word_fragment_matches_ehitusnael(self):
        target = self.offer("Ehitusnael 3,1x100 mm", category=self.fasteners, sku="NAEL-WORD")
        nail_gun = self.offer("Naelapüstol 18 V", category=self.fasteners, sku="NAEL-GUN")

        results = search_products("nael")
        result_ids = self.result_ids(results)

        self.assertIn(target.pk, result_ids)
        self.assertNotIn(nail_gun.pk, result_ids)

    def test_text_token_matches_word_or_compound_suffix_only(self):
        separate_word = self.offer("Puitm. - Pruss 47x50x4800mm", sku="PRUSS-WORD")
        compound_suffix = self.offer("Höövelpruss 50x50x3000", sku="PRUSS-SUFFIX")
        prefix_only = self.offer("Prussakalõks Arox 2tk", sku="PRUSS-TRAP")

        results = search_products("pruss")
        result_ids = self.result_ids(results)

        self.assertIn(separate_word.pk, result_ids)
        self.assertIn(compound_suffix.pk, result_ids)
        self.assertNotIn(prefix_only.pk, result_ids)

    def test_exact_pruss_dimensions_rank_above_other_dimensions(self):
        exact_size = self.offer("Pruss 50x50x3000 mm", sku="RANK-PRUSS-50", price="20.00")
        other_size = self.offer("Pruss 47x50x4800 mm", sku="RANK-PRUSS-47", price="1.00")

        result_ids = self.result_ids(search_products("pruss 50x50"))

        self.assertLess(result_ids.index(exact_size.pk), result_ids.index(other_size.pk))

    def test_exact_word_ranks_above_compound_suffix_for_same_dimensions(self):
        exact_word = self.offer("Pruss 50x50x3000 mm", sku="RANK-WORD", price="20.00")
        compound = self.offer("Höövelpruss 50x50x3000 mm", sku="RANK-COMPOUND", price="1.00")

        result_ids = self.result_ids(search_products("pruss 50x50"))

        self.assertLess(result_ids.index(exact_word.pk), result_ids.index(compound.pk))

    def test_number_in_dimension_ranks_above_same_number_as_quantity(self):
        exact_length = self.offer("Ehitusnael 4x100 mm", sku="RANK-NAEL-100", price="20.00")
        other_length = self.offer("Ehitusnael 4x90 mm 100 tk", sku="RANK-NAEL-90", price="1.00")

        result_ids = self.result_ids(search_products("nael 100"))

        self.assertLess(result_ids.index(exact_length.pk), result_ids.index(other_length.pk))

    def test_exact_barcode_ranks_first_even_when_other_product_is_cheaper(self):
        exact = self.offer(
            "Bosch drill GSR 18V",
            barcode="4740000000001",
            sku="RANK-BARCODE",
            brand="Bosch",
            model="GSR18V",
            price="20.00",
        )
        self.offer(
            "Bosch drill GSR 18V budget",
            sku="RANK-BUDGET",
            brand="Bosch",
            model="GSR18V",
            price="1.00",
        )

        result_ids = self.result_ids(search_products("4740000000001"))

        self.assertEqual(result_ids[0], exact.pk)

    def test_weak_fuzzy_match_does_not_outrank_exact_words_and_dimensions(self):
        exact = self.offer("Pruss 50x50x3000 mm", sku="RANK-EXACT", price="20.00")
        fuzzy = self.offer("Pruzz 50x50x3000 mm", sku="RANK-FUZZY", price="1.00")

        result_ids = self.result_ids(search_products("pruss 50x50"))

        self.assertEqual(result_ids[0], exact.pk)
        self.assertNotIn(fuzzy.pk, result_ids[:1])

    def test_equal_relevance_has_stable_order(self):
        first = self.offer("Pruss 50x50x3000 mm", sku="RANK-STABLE-1", price="10.00")
        second = self.offer("Pruss 50x50x3000 mm", sku="RANK-STABLE-2", price="10.00")

        first_search = self.result_ids(search_products("pruss 50x50"))
        second_search = self.result_ids(search_products("pruss 50x50"))

        self.assertEqual(first_search, second_search)
        self.assertEqual(first_search[:2], [first.pk, second.pk])

    def test_equal_relevance_sorts_by_price_across_shops(self):
        expensive = self.offer("Pruss 45x45x3000 mm", sku="PRICE-6", price="6.00")
        cheap = self.offer(
            "Pruss 45x45x3600 mm",
            shop=self.depo,
            category=None,
            sku="PRICE-3",
            price="3.00",
        )

        result_ids = self.result_ids(search_products("pruss"))

        self.assertLess(result_ids.index(cheap.pk), result_ids.index(expensive.pk))

    def test_sale_price_is_used_for_equal_relevance_sorting(self):
        regular = self.offer("Pruss 45x45x3000 mm", sku="REGULAR-3", price="3.00")
        discounted = self.offer(
            "Pruss 45x45x3600 mm",
            shop=self.depo,
            category=None,
            sku="SALE-2",
            price="10.00",
            sale_price="2.00",
        )

        result_ids = self.result_ids(search_products("pruss"))

        self.assertLess(result_ids.index(discounted.pk), result_ids.index(regular.pk))

    def test_offer_without_price_is_last_within_relevance_level(self):
        missing_price = self.offer("Pruss 45x45x3000 mm", sku="NO-PRICE", price=None)
        priced = self.offer(
            "Pruss 45x45x3600 mm",
            shop=self.depo,
            category=None,
            sku="WITH-PRICE",
            price="100.00",
        )

        result_ids = self.result_ids(search_products("pruss"))

        self.assertLess(result_ids.index(priced.pk), result_ids.index(missing_price.pk))

    def test_partial_dimensions_match_full_hoovelpruss_dimensions(self):
        target = self.offer("Höövelpruss 50x50x3000 mm", sku="PRUSS-50")
        wrong_size = self.offer("Höövelpruss 150x50x3000 mm", sku="PRUSS-150")

        results = search_products("pruss 50x50")
        result_ids = self.result_ids(results)

        self.assertIn(target.pk, result_ids)
        self.assertLess(result_ids.index(target.pk), result_ids.index(wrong_size.pk))

    def test_spaced_and_multiplication_sign_dimensions_are_equivalent(self):
        target = self.offer("Höövelpruss 50x50x3000 mm", sku="PRUSS-VARIANTS")

        compact = self.result_ids(search_products("pruss 50x50"))
        spaced = self.result_ids(search_products("pruss 50 x 50"))
        multiplication_sign = self.result_ids(search_products("pruss 50×50"))

        self.assertIn(target.pk, compact)
        self.assertEqual(compact, spaced)
        self.assertEqual(compact, multiplication_sign)

    def test_number_token_does_not_match_inside_larger_number(self):
        exact_number = self.offer("Höövelpruss 50x50x3000 mm", sku="NUMBER-50")
        larger_number = self.offer("Höövelpruss 150x150x3000 mm", sku="NUMBER-150")
        exact_hundred = self.offer("Ehitusnael 3,1x100 mm", sku="NUMBER-100")
        larger_hundred = self.offer("Ehitusnael 3,1x1000 mm", sku="NUMBER-1000")

        fifty_ids = self.result_ids(search_products("50"))
        hundred_ids = self.result_ids(search_products("100"))

        self.assertIn(exact_number.pk, fifty_ids)
        self.assertNotIn(larger_number.pk, fifty_ids)
        self.assertIn(exact_hundred.pk, hundred_ids)
        self.assertNotIn(larger_hundred.pk, hundred_ids)

    def test_barcode_search_remains_exact(self):
        target = self.offer("Exact barcode product", barcode="4740000000001", sku="BARCODE-EXACT")
        longer = self.offer("Longer barcode product", barcode="147400000000010", sku="BARCODE-LONGER")

        results = search_products("4740000000001")

        self.assertIn(target.pk, self.ids(results.exact_matches))
        self.assertNotIn(longer.pk, self.result_ids(results))

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
    def test_normalization_keeps_words_separate_from_measurements(self):
        self.assertEqual(normalize_product_name("nael 100"), "nael 100")
        self.assertEqual(normalize_product_name("pruss 50x50"), "pruss 50x50")
        self.assertEqual(normalize_product_name("pruss 50 x 50"), "pruss 50x50")
        self.assertEqual(normalize_product_name("pruss 50×50"), "pruss 50x50")

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
