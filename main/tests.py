from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse

from catalog.models import Category, Product, ProductOffer, Shop


@override_settings(
    STORAGES={
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
)
class MainCatalogTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="ESPAK", code="espak")
        self.other_shop = Shop.objects.create(name="DEPO", code="depo")
        self.category = Category.objects.create(
            shop=self.shop,
            external_id="tools",
            name="Инструменты",
        )
        self.other_category = Category.objects.create(
            shop=self.other_shop,
            external_id="garden",
            name="Сад",
        )

    def create_offer(
        self,
        *,
        name="Bosch drill",
        shop=None,
        category=None,
        sku="SKU-1",
        barcode="EAN-1",
        external_id="offer-1",
        brand="Bosch",
        model="GSR 18V-50",
        price=Decimal("12.99"),
        sale_price=None,
        description="Compact drill for home projects.",
        is_active=True,
        is_available=True,
    ):
        shop = shop or self.shop
        category = self.category if category is None and shop == self.shop else category
        product = Product.objects.create(
            name=name,
            brand=brand,
            model=model,
            barcode=barcode,
        )
        return ProductOffer.objects.create(
            shop=shop,
            product=product,
            category=category,
            external_id=external_id,
            sku=sku,
            barcode=barcode,
            original_name=name,
            description=description,
            price=price,
            sale_price=sale_price,
            currency="EUR",
            image_url="https://example.com/image.jpg",
            product_url="https://example.com/product",
            is_active=is_active,
            is_available=is_available,
        )

    def catalog_offer_ids(self, response):
        return [offer.pk for offer in response.context["page_obj"].object_list]

    def test_home_page_opens(self):
        response = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Сравнивайте цены")

    def test_home_search_form_submits_to_product_search(self):
        response = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")

        self.assertContains(response, f'action="{reverse("product_search")}"')
        self.assertContains(response, "Открыть каталог")

    def test_product_search_page_opens_without_query(self):
        response = self.client.get(reverse("product_search"), HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter product name, SKU or barcode")

    def test_product_search_groups_exact_barcode_and_same_product(self):
        exact = self.create_offer(name="Makita DDF482Z drill", barcode="4000000000001", brand="Makita", model="DDF482Z")
        same = self.create_offer(
            name="Akutrell MAKITA DDF 482 Z",
            shop=self.other_shop,
            category=self.other_category,
            sku="DEPO-DDF482",
            barcode="",
            external_id="depo-ddf482",
            brand="Makita",
            model="DDF482Z",
        )

        response = self.client.get(reverse("product_search"), {"q": "4000000000001"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Exact match")
        self.assertContains(response, "Same product in other stores")
        self.assertContains(response, exact.original_name)
        self.assertContains(response, same.original_name)

    def test_catalog_page_opens(self):
        response = self.client.get(reverse("catalog"), HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Каталог товаров")

    def test_catalog_shows_only_active_and_available_offers(self):
        visible = self.create_offer(name="Visible Bosch", external_id="visible")
        self.create_offer(name="Inactive Bosch", external_id="inactive", is_active=False)
        self.create_offer(name="Unavailable Bosch", external_id="unavailable", is_available=False)

        response = self.client.get(reverse("catalog"), {"q": "Bosch"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(self.catalog_offer_ids(response), [visible.pk])
        self.assertContains(response, "Visible Bosch")
        self.assertNotContains(response, "Inactive Bosch")
        self.assertNotContains(response, "Unavailable Bosch")

    def test_catalog_search_by_original_name(self):
        bosch = self.create_offer(name="Bosch drill", external_id="bosch")
        self.create_offer(
            name="Makita saw",
            sku="SKU-2",
            barcode="EAN-2",
            external_id="makita",
            brand="Makita",
        )

        response = self.client.get(reverse("catalog"), {"q": "bosch"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(self.catalog_offer_ids(response), [bosch.pk])

    def test_catalog_search_is_order_independent_for_tokens(self):
        offer = self.create_offer(
            name="AKULÖÖKTRELL MAKITA DHP482Z",
            external_id="makita-impact-drill",
            brand="Makita",
            model="DHP482Z",
        )

        direct = self.client.get(reverse("catalog"), {"q": "trell makita"}, HTTP_HOST="127.0.0.1")
        reversed_query = self.client.get(reverse("catalog"), {"q": "makita trell"}, HTTP_HOST="127.0.0.1")

        self.assertIn(offer.pk, self.catalog_offer_ids(direct))
        self.assertIn(offer.pk, self.catalog_offer_ids(reversed_query))

    def test_catalog_search_by_sku(self):
        offer = self.create_offer(name="Angle grinder", sku="SKU-BOSCH-42")

        response = self.client.get(reverse("catalog"), {"q": "SKU-BOSCH-42"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(self.catalog_offer_ids(response), [offer.pk])

    def test_catalog_search_by_barcode(self):
        offer = self.create_offer(name="Barcode product", barcode="EAN-BOSCH-42")

        response = self.client.get(reverse("catalog"), {"q": "EAN-BOSCH-42"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(self.catalog_offer_ids(response), [offer.pk])

    def test_catalog_filters_by_shop_code(self):
        self.create_offer(name="ESPAK drill", external_id="espak-drill")
        depo_offer = self.create_offer(
            name="DEPO drill",
            shop=self.other_shop,
            category=self.other_category,
            sku="SKU-DEPO",
            barcode="EAN-DEPO",
            external_id="depo-drill",
        )

        response = self.client.get(reverse("catalog"), {"shop": "depo"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(self.catalog_offer_ids(response), [depo_offer.pk])

    def test_catalog_filters_by_category_id(self):
        self.create_offer(name="Tool offer", external_id="tool")
        garden_offer = self.create_offer(
            name="Garden offer",
            shop=self.other_shop,
            category=self.other_category,
            sku="SKU-GARDEN",
            barcode="EAN-GARDEN",
            external_id="garden",
        )

        response = self.client.get(
            reverse("catalog"),
            {"category": str(self.other_category.pk)},
            HTTP_HOST="127.0.0.1",
        )

        self.assertEqual(self.catalog_offer_ids(response), [garden_offer.pk])

    def test_catalog_name_asc_sorting(self):
        alpha = self.create_offer(name="Alpha", external_id="alpha")
        zeta = self.create_offer(name="Zeta", sku="SKU-Z", barcode="EAN-Z", external_id="zeta")

        response = self.client.get(reverse("catalog"), {"sort": "name_asc"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(self.catalog_offer_ids(response), [alpha.pk, zeta.pk])

    def test_catalog_price_asc_uses_sale_price(self):
        regular = self.create_offer(name="Regular", external_id="regular", price=Decimal("10.00"))
        discounted = self.create_offer(
            name="Discounted",
            sku="SKU-DISC",
            barcode="EAN-DISC",
            external_id="discounted",
            price=Decimal("20.00"),
            sale_price=Decimal("5.00"),
        )
        no_price = self.create_offer(
            name="No price",
            sku="SKU-NO",
            barcode="EAN-NO",
            external_id="no-price",
            price=None,
        )

        response = self.client.get(reverse("catalog"), {"sort": "price_asc"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(self.catalog_offer_ids(response), [discounted.pk, regular.pk, no_price.pk])

    def test_catalog_price_desc_uses_sale_price(self):
        cheap = self.create_offer(name="Cheap", external_id="cheap", price=Decimal("3.00"))
        expensive = self.create_offer(
            name="Expensive",
            sku="SKU-EXP",
            barcode="EAN-EXP",
            external_id="expensive",
            price=Decimal("20.00"),
        )
        no_price = self.create_offer(
            name="No price",
            sku="SKU-NO",
            barcode="EAN-NO",
            external_id="no-price",
            price=None,
        )

        response = self.client.get(reverse("catalog"), {"sort": "price_desc"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(self.catalog_offer_ids(response), [expensive.pk, cheap.pk, no_price.pk])

    def test_catalog_paginates_by_24_products(self):
        for index in range(30):
            self.create_offer(
                name=f"Bosch product {index:02d}",
                sku=f"SKU-{index}",
                barcode=f"EAN-{index}",
                external_id=f"offer-{index}",
            )

        response = self.client.get(reverse("catalog"), {"q": "Bosch"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(len(response.context["page_obj"].object_list), 24)
        self.assertEqual(response.context["page_obj"].paginator.count, 30)

    def test_catalog_invalid_page_does_not_error(self):
        self.create_offer(name="Bosch drill")

        response = self.client.get(reverse("catalog"), {"page": "bad"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 200)

    def test_catalog_invalid_shop_does_not_error(self):
        self.create_offer(name="Bosch drill")

        response = self.client.get(reverse("catalog"), {"shop": "missing"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bosch drill")

    def test_catalog_invalid_category_does_not_error(self):
        self.create_offer(name="Bosch drill")

        response = self.client.get(reverse("catalog"), {"category": "not-a-number"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bosch drill")

    def test_catalog_pagination_links_keep_get_params(self):
        for index in range(30):
            self.create_offer(
                name=f"Bosch product {index:02d}",
                sku=f"SKU-{index}",
                barcode=f"EAN-{index}",
                external_id=f"offer-{index}",
            )

        response = self.client.get(
            reverse("catalog"),
            {"q": "Bosch", "shop": "espak", "category": str(self.category.pk), "sort": "name_asc"},
            HTTP_HOST="127.0.0.1",
        )

        self.assertContains(
            response,
            f"q=Bosch&amp;shop=espak&amp;category={self.category.pk}&amp;sort=name_asc&amp;page=2",
        )

    def test_catalog_card_links_to_offer_detail(self):
        offer = self.create_offer(name="Bosch drill")

        response = self.client.get(reverse("catalog"), {"q": "Bosch"}, HTTP_HOST="127.0.0.1")

        self.assertContains(response, reverse("offer_detail", args=[offer.pk]))
        self.assertContains(response, "Подробнее")

    def test_offer_detail_active_available_offer_opens(self):
        offer = self.create_offer(name="Bosch drill")

        response = self.client.get(reverse("offer_detail", args=[offer.pk]), HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bosch drill")

    def test_offer_detail_inactive_offer_returns_404(self):
        offer = self.create_offer(name="Inactive Bosch", is_active=False)

        response = self.client.get(reverse("offer_detail", args=[offer.pk]), HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 404)

    def test_offer_detail_unavailable_offer_returns_404(self):
        offer = self.create_offer(name="Unavailable Bosch", is_available=False)

        response = self.client.get(reverse("offer_detail", args=[offer.pk]), HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 404)

    def test_offer_detail_contains_price_and_store_link(self):
        self.create_offer(name="Bosch drill", sale_price=Decimal("9.99"))

        response = self.client.get(reverse("offer_detail", args=[ProductOffer.objects.get().pk]), HTTP_HOST="127.0.0.1")

        self.assertContains(response, "9.99 EUR")
        self.assertContains(response, "12.99 EUR")
        self.assertContains(response, 'href="https://example.com/product"')
        self.assertContains(response, 'rel="noopener noreferrer"')

    def test_suggestions_short_query_returns_empty_list(self):
        self.create_offer(name="Bosch drill")

        response = self.client.get(reverse("search_suggestions"), {"q": "b"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"results": []})

    def test_suggestions_search_by_name_works(self):
        self.create_offer(name="Bosch drill")

        response = self.client.get(reverse("search_suggestions"), {"q": "bosch"}, HTTP_HOST="127.0.0.1")

        payload = response.json()
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["name"], "Bosch drill")

    def test_suggestions_search_is_order_independent_for_tokens(self):
        offer = self.create_offer(
            name="AKULÖÖKTRELL MAKITA DHP482Z",
            external_id="suggestion-makita-impact-drill",
            brand="Makita",
            model="DHP482Z",
        )

        direct = self.client.get(reverse("search_suggestions"), {"q": "trell makita"}, HTTP_HOST="127.0.0.1")
        reversed_query = self.client.get(reverse("search_suggestions"), {"q": "makita trell"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(direct.json()["results"][0]["id"], offer.pk)
        self.assertEqual(reversed_query.json()["results"][0]["id"], offer.pk)

    def test_suggestions_search_by_sku_works(self):
        self.create_offer(name="Angle grinder", sku="SKU-BOSCH-42")

        response = self.client.get(
            reverse("search_suggestions"),
            {"q": "SKU-BOSCH-42"},
            HTTP_HOST="127.0.0.1",
        )

        self.assertEqual(response.json()["results"][0]["name"], "Angle grinder")

    def test_suggestions_inactive_offer_is_hidden(self):
        self.create_offer(name="Inactive Bosch", is_active=False)

        response = self.client.get(reverse("search_suggestions"), {"q": "Inactive"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(response.json(), {"results": []})

    def test_suggestions_unavailable_offer_is_hidden(self):
        self.create_offer(name="Unavailable Bosch", is_available=False)

        response = self.client.get(reverse("search_suggestions"), {"q": "Unavailable"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(response.json(), {"results": []})

    def test_suggestions_are_limited_to_8(self):
        for index in range(12):
            self.create_offer(
                name=f"Bosch product {index:02d}",
                sku=f"SKU-SUG-{index}",
                barcode=f"EAN-SUG-{index}",
                external_id=f"suggestion-{index}",
            )

        response = self.client.get(reverse("search_suggestions"), {"q": "Bosch"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(len(response.json()["results"]), 8)

    def test_suggestions_json_contains_expected_fields(self):
        offer = self.create_offer(name="Bosch drill", sku="SKU-1", barcode="EAN-1", sale_price=Decimal("9.99"))

        response = self.client.get(reverse("search_suggestions"), {"q": "Bosch"}, HTTP_HOST="127.0.0.1")

        result = response.json()["results"][0]
        self.assertEqual(
            set(result),
            {
                "id",
                "name",
                "shop",
                "category",
                "sku",
                "barcode",
                "price",
                "sale_price",
                "currency",
                "image_url",
                "product_url",
                "detail_url",
            },
        )
        self.assertEqual(result["price"], "12.99")
        self.assertEqual(result["sale_price"], "9.99")
        self.assertEqual(result["detail_url"], reverse("offer_detail", args=[offer.pk]))

    def test_suggestion_detail_url_matches_offer(self):
        offer = self.create_offer(name="Bosch drill")

        response = self.client.get(reverse("search_suggestions"), {"q": "Bosch"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(response.json()["results"][0]["detail_url"], f"/offer/{offer.pk}/")
