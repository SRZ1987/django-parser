from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse

from catalog.models import Product, ProductOffer, Shop


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
class HomeSearchTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="ESPAK", code="espak")

    def create_offer(
        self,
        *,
        name="Bosch drill",
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
        product = Product.objects.create(
            name=name,
            brand=brand,
            model=model,
            barcode=barcode,
        )
        return ProductOffer.objects.create(
            shop=self.shop,
            product=product,
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

    def test_home_page_opens(self):
        response = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Найдите товар")

    def test_empty_search_does_not_return_all_products(self):
        self.create_offer(name="Bosch drill")

        response = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Введите название, SKU или штрихкод")
        self.assertNotContains(response, "Bosch drill")
        self.assertEqual(list(response.context["offers"]), [])

    def test_search_by_name_works(self):
        self.create_offer(name="Bosch drill")
        self.create_offer(
            name="Makita saw",
            sku="SKU-2",
            barcode="EAN-2",
            external_id="offer-2",
            brand="Makita",
        )

        response = self.client.get(reverse("home"), {"q": "bosch"}, HTTP_HOST="127.0.0.1")

        self.assertContains(response, "Bosch drill")
        self.assertNotContains(response, "Makita saw")

    def test_search_by_sku_works(self):
        self.create_offer(name="Angle grinder", sku="SKU-BOSCH-42")

        response = self.client.get(reverse("home"), {"q": "SKU-BOSCH-42"}, HTTP_HOST="127.0.0.1")

        self.assertContains(response, "Angle grinder")

    def test_inactive_offer_is_hidden(self):
        self.create_offer(name="Inactive Bosch", is_active=False)

        response = self.client.get(reverse("home"), {"q": "Inactive"}, HTTP_HOST="127.0.0.1")

        self.assertContains(response, "Ничего не найдено")
        self.assertNotContains(response, "Inactive Bosch")

    def test_unavailable_offer_is_hidden(self):
        self.create_offer(name="Unavailable Bosch", is_available=False)

        response = self.client.get(reverse("home"), {"q": "Unavailable"}, HTTP_HOST="127.0.0.1")

        self.assertContains(response, "Ничего не найдено")
        self.assertNotContains(response, "Unavailable Bosch")

    def test_results_are_limited_to_50(self):
        for index in range(60):
            self.create_offer(
                name=f"Bosch product {index:02d}",
                sku=f"SKU-{index}",
                barcode=f"EAN-{index}",
                external_id=f"offer-{index}",
            )

        response = self.client.get(reverse("home"), {"q": "Bosch"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(len(response.context["offers"]), 50)

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
        offer = self.create_offer(name="Bosch drill", sale_price=Decimal("9.99"))

        response = self.client.get(reverse("offer_detail", args=[offer.pk]), HTTP_HOST="127.0.0.1")

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
