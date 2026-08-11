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
    offers_are_comparable,
    score_offer_against_query,
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
        description="",
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
            description=description,
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

    def test_barcode_search_puts_exact_barcode_before_normalized_name_matches(self):
        source = self.offer(
            "Trimmerijõhv Oregon 2.0mm 15m",
            barcode="4740000000025",
            sku="OREGON-SOURCE",
            price="8.00",
        )
        same_barcode = self.offer(
            "Oregon trimmerijõhv 2mm 15m",
            shop=self.depo,
            barcode="4740000000025",
            sku="OREGON-EAN",
            price="6.00",
        )
        similar_name = self.offer(
            "Trimmerijõhv Oregon 2mm 15m",
            shop=self.bauhof,
            barcode="",
            sku="OREGON-NAME",
            price="3.00",
        )
        brand_only = self.offer(
            "Akrüülrull Oregon 15x70x4mm",
            shop=self.depo,
            barcode="",
            sku="OREGON-BRAND-ONLY",
            brand="Oregon",
            price="1.00",
        )

        result_ids = self.result_ids(search_products("4740000000025"))

        self.assertEqual(set(result_ids[:2]), {source.pk, same_barcode.pk})
        self.assertIn(similar_name.pk, result_ids[2:])
        self.assertNotIn(brand_only.pk, result_ids)

    def test_sku_search_expands_through_barcode_and_normalized_name(self):
        source = self.offer(
            "Trimmerijõhv Oregon 2mm 15m",
            barcode="4740000000032",
            sku="OREGON-SKU-32",
            price="8.00",
        )
        same_barcode = self.offer(
            "Oregon trimmerijõhv 2.0mm 15m",
            shop=self.depo,
            barcode="4740000000032",
            sku="DEPO-32",
            price="6.00",
        )
        similar_name = self.offer(
            "Trimmerijõhv Oregon 2,0 mm 15m",
            shop=self.bauhof,
            barcode="",
            sku="BAUHOF-32",
            price="3.00",
        )

        result_ids = self.result_ids(search_products("OREGON-SKU-32"))

        self.assertEqual(set(result_ids[:2]), {source.pk, same_barcode.pk})
        self.assertIn(similar_name.pk, result_ids[2:])

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

        self.assertIn(same_size.pk, ranked_ids)
        self.assertNotIn(other_size.pk, ranked_ids)

    def test_package_quantity_influences_ranking(self):
        pack_100 = self.offer("Screw 5x70mm 100 pcs", category=self.fasteners, sku="PACK100")
        pack_1000 = self.offer("Screw 5x70mm 1000 pcs", category=self.fasteners, sku="PACK1000")

        results = search_products("screw 5x70 100 pcs")
        ranked_ids = self.result_ids(results)

        self.assertIn(pack_100.pk, ranked_ids)
        self.assertNotIn(pack_1000.pk, ranked_ids)

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
        self.assertNotIn(makita_partial.pk, reversed_ids)

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
        self.assertNotIn(other_size.pk, direct_ids)
        self.assertNotIn(other_size.pk, reversed_ids)

    def test_multitoken_text_query_requires_every_token(self):
        compound = self.offer(
            "Bensiinimootoriga murutrimmer Jasper 32.5cm3 0.7kW 44cm",
            sku="BENSIINI-COMPOUND",
        )
        separate = self.offer("Bensiini murutrimmer 43cm", sku="BENSIINI-SEPARATE")
        generator = self.offer("Generaator, bensiini, 2kW", sku="BENSIINI-GENERATOR")
        spool = self.offer("SPOONI SERVATRIMMER", sku="TRIMMER-SPOOL")
        hair_trimmer = self.offer("Juukselõikur-trimmer", sku="HAIR-TRIMMER")

        results = search_products("bensiini trimmer")
        result_ids = self.result_ids(results)

        self.assertIn(compound.pk, result_ids)
        self.assertIn(separate.pk, result_ids)
        self.assertNotIn(generator.pk, result_ids)
        self.assertNotIn(spool.pk, result_ids)
        self.assertNotIn(hair_trimmer.pk, result_ids)
        self.assertEqual(results.candidates_count, 2)

        partial_match = score_offer_against_query(generator, "bensiini trimmer")
        self.assertEqual(partial_match.score, 0.0)
        self.assertIn("not all query tokens matched", partial_match.reasons)

    def test_absent_barcode_and_brand_still_search_by_name(self):
        offer = self.offer("Generic screw 5x70mm", barcode="", brand="", model="", category=self.fasteners)

        results = search_products("generic 5x70")

        self.assertIn(offer.pk, self.ids(results.similar_products + results.same_product))

    def test_compound_word_and_number_match_ehitusnael(self):
        target = self.offer("Ehitusnael 3,1x100 mm", category=self.fasteners, sku="NAEL-100")
        wrong_length = self.offer("Ehitusnael 3,1x1000 mm", category=self.fasteners, sku="NAEL-1000")
        number_only = self.offer("Kruvi 4x100 mm", category=self.fasteners, sku="NO-NAEL-100")

        results = search_products("nael 100")
        result_ids = self.result_ids(results)

        self.assertIn(target.pk, result_ids)
        self.assertNotIn(wrong_length.pk, result_ids)
        self.assertNotIn(number_only.pk, result_ids)

    def test_ehitusnael_query_requires_requested_length(self):
        wrong_length = self.offer(
            "Ehitusnaelad SUKI international Ø1.2xL20mm/85g hele",
            category=self.fasteners,
            sku="SUKI-20",
        )
        correct_length = self.offer(
            "Ehitusnaelad SUKI international Ø3.1xL100mm/1kg",
            category=self.fasteners,
            sku="SUKI-100",
        )

        result_ids = self.result_ids(search_products("ehitusnael 100"))

        self.assertIn(correct_length.pk, result_ids)
        self.assertNotIn(wrong_length.pk, result_ids)

    def test_product_type_and_power_are_both_required(self):
        trimmer = self.offer("Elektrimootoriga murutrimmer Jasper 350W/25cm", sku="TRIMMER-350")
        wrong_power = self.offer("Murutrimmer Jasper 500W/25cm", sku="TRIMMER-500")
        jigsaw = self.offer("Elektriline tikksaag Jasper 350W", sku="JIGSAW-350")

        result_ids = self.result_ids(search_products("murutrimmer 350w"))

        self.assertIn(trimmer.pk, result_ids)
        self.assertNotIn(wrong_power.pk, result_ids)
        self.assertNotIn(jigsaw.pk, result_ids)

    def test_equivalent_power_and_weight_units_match(self):
        trimmer = self.offer("Murutrimmer 700W", sku="TRIMMER-700W")
        filler = self.offer("Pahtel 1000g", sku="FILLER-1000G")
        self.offer("Murutrimmer 750W", sku="TRIMMER-750W")
        self.offer("Pahtel 1500g", sku="FILLER-1500G")

        self.assertIn(trimmer.pk, self.result_ids(search_products("murutrimmer 0.7kw")))
        self.assertIn(filler.pk, self.result_ids(search_products("pahtel 1kg")))

    def test_offer_comparison_rejects_conflicting_dimensions_and_weight(self):
        source = self.offer("Ehitusnael 3.1x100mm 1kg", sku="COMPARE-SOURCE")
        equivalent = self.offer(
            "Ehitusnaelad 3.1x100mm 1000g",
            shop=self.depo,
            category=None,
            sku="COMPARE-EQUAL",
        )
        wrong_length = self.offer(
            "Ehitusnaelad 3.1x90mm 1000g",
            shop=self.bauhof,
            category=None,
            sku="COMPARE-LENGTH",
        )
        wrong_weight = self.offer(
            "Ehitusnaelad 3.1x100mm 500g",
            shop=self.bauhof,
            category=None,
            sku="COMPARE-WEIGHT",
        )
        wrong_type = self.offer(
            "Kruvi SUKI international 3.1x100mm 1kg",
            shop=self.depo,
            category=None,
            sku="COMPARE-TYPE",
        )

        self.assertTrue(offers_are_comparable(source, equivalent))
        self.assertFalse(offers_are_comparable(source, wrong_length))
        self.assertFalse(offers_are_comparable(source, wrong_weight))
        self.assertFalse(offers_are_comparable(source, wrong_type))

    def test_offer_comparison_rejects_real_nail_names_with_different_dimensions(self):
        source = self.offer(
            "EHITUSNAEL HJFASTENERS 4,0X100 5KG CA. 496TK PAKIS",
            sku="BAUHOF-NAEL-4X100",
        )
        wrong_dimensions = self.offer(
            "Ehitusnaelad/tsingitud Metalo Prekyba Ø2xL40mm 5kg",
            shop=self.depo,
            category=None,
            sku="DEPO-NAEL-2X40",
        )

        self.assertFalse(offers_are_comparable(source, wrong_dimensions))

    def test_offer_comparison_rejects_candidate_without_source_dimensions(self):
        source = self.offer(
            "EHITUSNAEL HJFASTENERS 4,0X100 5KG CA. 496TK PAKIS",
            sku="BAUHOF-NAEL-WITH-DIMENSIONS",
        )
        missing_dimensions = self.offer(
            "Ehitusnael pinnakatteta",
            shop=self.depo,
            category=None,
            sku="HAMMERJACK-NAEL-WITHOUT-DIMENSIONS",
        )

        self.assertFalse(offers_are_comparable(source, missing_dimensions))

    def test_offer_comparison_rejects_accessory_for_main_product(self):
        source = self.offer(
            "Elektrimootoriga murutrimmer QT6045 Jasper 350W/25cm",
            sku="DEPO-MURUTRIMMER",
        )
        shoulder_strap = self.offer(
            "Õlarihm murutrimmeritele",
            shop=self.depo,
            category=None,
            sku="HAMMERJACK-TRIMMER-STRAP",
        )

        self.assertFalse(offers_are_comparable(source, shoulder_strap))

    def test_powered_tool_can_match_when_secondary_size_is_missing(self):
        source = self.offer(
            "Elektrimootoriga murutrimmer QT6045 Jasper 350W/25cm",
            sku="DEPO-MURUTRIMMER-WITH-WIDTH",
        )
        same_type_and_power = self.offer(
            "Murutrimmer Trolla 350W",
            shop=self.depo,
            category=None,
            sku="HANDYMANN-MURUTRIMMER-350W",
        )
        conflicting_width = self.offer(
            "Murutrimmer Trolla 350W/30cm",
            shop=self.bauhof,
            category=None,
            sku="BAUHOF-MURUTRIMMER-350W-30CM",
        )

        self.assertTrue(offers_are_comparable(source, same_type_and_power))
        self.assertFalse(offers_are_comparable(source, conflicting_width))

    def test_cleaners_for_different_surfaces_are_not_comparable(self):
        source = self.offer(
            "Puhastusvahend puidule Pinotex Terrace&Wood Cleaner 5L",
            sku="DECORA-PINOTEX-WOOD-CLEANER",
        )
        wood_cleaner = self.offer(
            "Terrassi ja puidu puhastusvahend 5 l",
            shop=self.bauhof,
            category=None,
            sku="BAUHOF-WOOD-CLEANER",
        )
        glass_cleaner = self.offer(
            "Klaasipuhastusvahend EWOL 5L",
            shop=self.depo,
            category=None,
            sku="DEPO-GLASS-CLEANER",
        )

        self.assertTrue(offers_are_comparable(source, wood_cleaner))
        self.assertFalse(offers_are_comparable(source, glass_cleaner))

    def test_same_model_does_not_override_product_type_and_power(self):
        source = self.offer(
            "Elektrimootoriga murutrimmer QT6045 Jasper 350W/25cm",
            sku="DEPO-MURUTRIMMER-QT6045",
        )
        compatible_spool = self.offer(
            "Trimmerijõhvi pooliga Jasper QT6045",
            shop=self.depo,
            category=None,
            sku="DEPO-QT6045-SPOOL",
        )

        self.assertFalse(offers_are_comparable(source, compatible_spool))

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
        dimensions_only = self.offer("Liimpuit 50x50x3000 mm", sku="RANK-NO-PRUSS", price="1.00")

        result_ids = self.result_ids(search_products("pruss 50x50"))

        self.assertIn(exact_size.pk, result_ids)
        self.assertNotIn(other_size.pk, result_ids)
        self.assertNotIn(dimensions_only.pk, result_ids)

    def test_text_search_sorts_by_price_before_compound_match_kind(self):
        exact_word = self.offer("Pruss 50x50x3000 mm", sku="RANK-WORD", price="20.00")
        compound = self.offer("Höövelpruss 50x50x3000 mm", sku="RANK-COMPOUND", price="1.00")

        result_ids = self.result_ids(search_products("pruss 50x50"))

        self.assertLess(result_ids.index(compound.pk), result_ids.index(exact_word.pk))

    def test_text_search_sorts_by_price_when_all_tokens_match(self):
        exact_length = self.offer("Ehitusnael 4x100 mm", sku="RANK-NAEL-100", price="20.00")
        other_length = self.offer("Ehitusnael 4x90 mm 100 tk", sku="RANK-NAEL-90", price="1.00")

        result_ids = self.result_ids(search_products("nael 100"))

        self.assertLess(result_ids.index(other_length.pk), result_ids.index(exact_length.pk))

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

    def test_barcode_has_priority_over_a_colliding_sku(self):
        barcode_match = self.offer(
            "Trimmerijõhv Oregon 2mm",
            barcode="4740000000049",
            sku="BARCODE-PRODUCT",
            price="10.00",
        )
        sku_collision = self.offer(
            "Unrelated cheap product",
            shop=self.depo,
            barcode="",
            sku="4740000000049",
            price="1.00",
        )

        result_ids = self.result_ids(search_products("4740000000049"))

        self.assertEqual(result_ids[0], barcode_match.pk)
        self.assertNotIn(sku_collision.pk, self.ids(search_products("4740000000049").exact_matches))

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

    def test_text_search_price_summary_matches_the_cheapest_visible_result(self):
        self.offer("Trimmerijõhv Oregon 2mm", sku="SUMMARY-REGULAR", price="8.00")
        cheapest = self.offer(
            "Varutrimmerijõhv Makita 2mm",
            shop=self.depo,
            sku="SUMMARY-CHEAPEST",
            price="10.00",
            sale_price="1.19",
        )

        results = search_products("trimmerijõhv")

        self.assertEqual(results.matches[0].offer.pk, cheapest.pk)
        self.assertEqual(results.price_summary.min_price, Decimal("1.19"))
        self.assertEqual(results.price_summary.cheapest_shop, self.depo.name)

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
        self.assertNotIn(wrong_size.pk, result_ids)

    def test_spaced_and_multiplication_sign_dimensions_are_equivalent(self):
        target = self.offer("Höövelpruss 50x50x3000 mm", sku="PRUSS-VARIANTS")

        compact = self.result_ids(search_products("pruss 50x50"))
        spaced = self.result_ids(search_products("pruss 50 x 50"))
        multiplication_sign = self.result_ids(search_products("pruss 50×50"))

        self.assertIn(target.pk, compact)
        self.assertEqual(compact, spaced)
        self.assertEqual(compact, multiplication_sign)

    def test_decimal_measurement_variants_return_the_same_products(self):
        integer = self.offer("Trimmerijõhv Oregon 2mm 15m", sku="JÕHV-2", price="3.00")
        decimal_dot = self.offer("Trimmerijõhv Makita 2.0mm 15m", sku="JÕHV-2-DOT", price="4.00")
        decimal_comma = self.offer("Trimmerijõhv Bosch 2,00 mm 15m", sku="JÕHV-2-COMMA", price="5.00")
        self.offer("Trimmerijõhv Oregon 2.4mm 15m", sku="JÕHV-24", price="1.00")

        ProductOffer.objects.filter(pk=decimal_dot.pk).update(
            normalized_name="trimmerijõhv makita 2.0mm 15m",
            search_text="trimmerijõhv makita 2.0mm 15m",
        )
        expected = [integer.pk, decimal_dot.pk, decimal_comma.pk]

        integer_query = self.result_ids(search_products("Trimmerijõhv 2mm"))
        dot_query = self.result_ids(search_products("Trimmerijõhv 2.0mm"))
        comma_query = self.result_ids(search_products("Trimmerijõhv 2,0 mm"))

        self.assertEqual(integer_query, expected)
        self.assertEqual(dot_query, expected)
        self.assertEqual(comma_query, expected)

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
        self.assertNotIn(longer.pk, self.ids(results.exact_matches))
        self.assertEqual(self.result_ids(results)[0], target.pk)

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

    def test_extracts_labeled_dimensions_product_type_and_weight(self):
        attributes = extract_product_attributes(
            "Ehitusnaelad SUKI international Ø1.2xL20mm/85g hele",
            category="Kinnitusvahendid / Ehitusnaelad",
        )

        self.assertIn("ehitusnael", attributes.product_type_tokens)
        self.assertEqual(attributes.dimensions, ("1.2mm", "20mm"))
        self.assertEqual(attributes.weight, "85g")

    def test_normalization_keeps_model_and_normalizes_units(self):
        self.assertEqual(normalize_product_name("Makita DDF 482 Z 18 V 5×70 мм"), "makita ddf482 z 18v 5x70mm")

    def test_extracts_extended_construction_measurements(self):
        attributes = extract_product_attributes(
            "Pump 3.6 m³/h 10 bar 1.2kW 230V 75dB"
        )

        self.assertEqual(
            attributes.measurements,
            frozenset({"3.6m3h", "10bar", "1.2kw", "230v", "75db"}),
        )

    def test_extracts_product_application_surface(self):
        wood_cleaner = extract_product_attributes(
            "Puhastusvahend puidule Pinotex Terrace&Wood Cleaner 5L"
        )
        glass_cleaner = extract_product_attributes("Klaasipuhastusvahend EWOL 5L")

        self.assertEqual(wood_cleaner.application_tokens, frozenset({"wood"}))
        self.assertEqual(glass_cleaner.application_tokens, frozenset({"glass"}))
        self.assertEqual(
            extract_product_attributes(
                "Kanalisatsioonipuhastusvahend Krots EWOL 5L"
            ).application_tokens,
            frozenset({"drain"}),
        )

    def test_normalizes_area_light_and_rotation_units(self):
        self.assertEqual(
            normalize_product_name("LED 1000 lm 4000 K 12 W 3600 p/min kaabel 2.5 mm²"),
            "led 1000lm 4000k 12w 3600rpm kaabel 2.5mm2",
        )
