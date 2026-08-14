import json
import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from catalog.models import Category, Product, ProductOffer, Shop
from main.email_verification import email_verification_token
from main.home_comparisons import get_home_price_comparisons
from main.models import (
    DailySiteVisit,
    GroupPurchase,
    GroupPurchaseMember,
    GroupPurchaseMessage,
    ShoppingListEvent,
    ShoppingListItem,
    StoreClick,
)
from main.price_alerts import send_shopping_list_price_alerts, set_shopping_list_price_alerts
from main.sitemaps import BarcodeComparisonSitemap
from main.services import add_offer_to_shopping_list, build_purchase_plan, get_best_offer


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
        cache.clear()
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
        quantity_price=None,
        quantity_price_min_quantity=None,
        description="Compact drill for home projects.",
        image_url="https://example.com/image.jpg",
        product_url="https://example.com/product",
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
            quantity_price=quantity_price,
            quantity_price_min_quantity=quantity_price_min_quantity,
            currency="EUR",
            image_url=image_url,
            product_url=product_url,
            is_active=is_active,
            is_available=is_available,
        )

    def catalog_offer_ids(self, response):
        return [offer.pk for offer in response.context["page_obj"].object_list]

    def create_user(self, username="user", password="StrongPass123"):
        return get_user_model().objects.create_user(username=username, password=password)

    def test_home_page_opens(self):
        response = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Поиск товаров")
        self.assertContains(response, "Tannenberg")

    def test_home_search_form_submits_to_product_search(self):
        response = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")

        self.assertContains(response, f'action="{reverse("product_search")}"')
        self.assertContains(response, "Название товара, SKU или штрихкод")

    def test_home_price_carousel_groups_same_barcode_and_sorts_by_price(self):
        expensive = self.create_offer(
            name="Makita DDF482Z drill",
            barcode="4000000000001",
            price=Decimal("119.00"),
        )
        cheaper = self.create_offer(
            name="Akutrell Makita DDF482Z",
            shop=self.other_shop,
            category=self.other_category,
            external_id="depo-ddf482-carousel",
            sku="DEPO-DDF482-CAROUSEL",
            barcode="4000000000001",
            price=Decimal("89.00"),
        )

        response = self.client.get(
            reverse("home"),
            HTTP_HOST="127.0.0.1",
            HTTP_ACCEPT_LANGUAGE="en",
        )

        comparison = next(
            group
            for group in response.context["price_comparisons"]
            if {offer["id"] for offer in group["offers"]} == {expensive.pk, cheaper.pk}
        )
        self.assertEqual([offer["id"] for offer in comparison["offers"]], [cheaper.pk, expensive.pk])
        self.assertContains(response, "data-price-carousel", html=False)
        self.assertContains(response, "DEPO")
        self.assertContains(response, "89.00 EUR")

    def test_home_price_carousel_does_not_group_names_without_matching_barcode(self):
        first = self.create_offer(
            name="Makita akutrell DDF482Z",
            barcode="",
            price=Decimal("100.00"),
        )
        second = self.create_offer(
            name="DDF482Z akutrell Makita",
            shop=self.other_shop,
            category=self.other_category,
            external_id="depo-name-order-carousel",
            sku="DEPO-NAME-ORDER",
            barcode="",
            price=Decimal("95.00"),
        )

        response = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")

        self.assertFalse(
            any(
                {offer["id"] for offer in group["offers"]} == {first.pk, second.pk}
                for group in response.context["price_comparisons"]
            )
        )

    def test_home_price_carousel_uses_lower_sale_price(self):
        regular = self.create_offer(
            name="Bosch carousel tool",
            barcode="4000000000002",
            price=Decimal("20.00"),
        )
        sale = self.create_offer(
            name="Bosch carousel tool",
            shop=self.other_shop,
            category=self.other_category,
            external_id="depo-sale-carousel",
            sku="DEPO-SALE-CAROUSEL",
            barcode="4000000000002",
            price=Decimal("25.00"),
            sale_price=Decimal("15.00"),
        )

        response = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")

        comparison = next(
            group
            for group in response.context["price_comparisons"]
            if {offer["id"] for offer in group["offers"]} == {regular.pk, sale.pk}
        )
        self.assertEqual([offer["id"] for offer in comparison["offers"]], [sale.pk, regular.pk])
        self.assertEqual(comparison["offers"][0]["price"], Decimal("15.00"))

    def test_home_price_carousel_does_not_group_partial_name_match(self):
        first = self.create_offer(name="Makita akutrell DDF482Z", barcode="")
        second = self.create_offer(
            name="Makita akutrell DDF482Z battery",
            shop=self.other_shop,
            category=self.other_category,
            external_id="depo-partial-name-carousel",
            sku="DEPO-PARTIAL-NAME",
            barcode="",
        )

        response = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")

        self.assertFalse(
            any(
                {offer["id"] for offer in group["offers"]} == {first.pk, second.pk}
                for group in response.context["price_comparisons"]
            )
        )

    def test_home_price_carousel_is_desktop_only(self):
        css_path = settings.BASE_DIR / "main" / "static" / "main" / "css" / "tannenberg.css"
        css = css_path.read_text(encoding="utf-8")

        self.assertIn(".price-carousel {\n    display: none;", css)
        self.assertIn("@media (min-width: 1025px)", css)

    def test_home_price_carousel_autoplays_before_full_width_search(self):
        self.create_offer(barcode="4000000000003")
        self.create_offer(
            name="Second store carousel offer",
            shop=self.other_shop,
            category=self.other_category,
            external_id="depo-autoplay-carousel",
            sku="DEPO-AUTOPLAY-CAROUSEL",
            barcode="4000000000003",
        )

        response = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")
        html = response.content.decode()
        css_path = settings.BASE_DIR / "main" / "static" / "main" / "css" / "tannenberg.css"
        javascript_path = (
            settings.BASE_DIR / "main" / "static" / "main" / "js" / "price-comparison-carousel.js"
        )

        self.assertLess(html.index("data-price-carousel"), html.index('id="page-title"'))
        self.assertLess(html.index('id="page-title"'), html.index('class="search-panel"'))
        self.assertNotIn("data-carousel-previous", html)
        self.assertNotIn("data-carousel-next", html)
        self.assertContains(response, "price-comparison-carousel.js?v=3", html=False)
        self.assertIn(".search-workspace .search-panel {\n    width: 100%;", css_path.read_text(encoding="utf-8"))
        javascript = javascript_path.read_text(encoding="utf-8")
        self.assertIn("window.setInterval(scrollToNextCard, 6500)", javascript)
        self.assertIn("const REFRESH_INTERVAL_MS = 30 * 60 * 1000", javascript)
        self.assertIn("await fetch(refreshUrl", javascript)
        self.assertIn("viewport.replaceChildren(nextTrack)", javascript)

    def test_home_price_carousel_rotates_groups_between_half_hour_buckets(self):
        for index in range(24):
            barcode = f"474000000{index:03d}"
            self.create_offer(
                name=f"Rotating product {index}",
                barcode=barcode,
                sku=f"ROTATE-A-{index}",
                external_id=f"rotate-a-{index}",
            )
            self.create_offer(
                name=f"Rotating product {index}",
                shop=self.other_shop,
                category=self.other_category,
                barcode=barcode,
                sku=f"ROTATE-B-{index}",
                external_id=f"rotate-b-{index}",
            )

        first_batch = get_home_price_comparisons(rotation_bucket=0)
        second_batch = get_home_price_comparisons(rotation_bucket=1)
        first_offer_ids = {group["detail_offer_id"] for group in first_batch}
        second_offer_ids = {group["detail_offer_id"] for group in second_batch}

        self.assertEqual(len(first_batch), 12)
        self.assertEqual(len(second_batch), 12)
        self.assertTrue(first_offer_ids.isdisjoint(second_offer_ids))

    def test_price_comparison_refresh_endpoint_returns_uncached_track(self):
        self.create_offer(barcode="4741234567890")
        self.create_offer(
            name="Second store refresh offer",
            shop=self.other_shop,
            category=self.other_category,
            external_id="depo-refresh-carousel",
            sku="DEPO-REFRESH-CAROUSEL",
            barcode="4741234567890",
        )

        response = self.client.get(reverse("price_comparisons"), HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="price-carousel-track"', html=False)
        self.assertContains(response, "Second store refresh offer")
        self.assertIn("no-cache", response.headers["Cache-Control"])

    def test_home_search_guide_is_unnumbered_centered_and_spaced(self):
        response = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")
        css_path = settings.BASE_DIR / "main" / "static" / "main" / "css" / "tannenberg.css"
        css = css_path.read_text(encoding="utf-8")

        self.assertNotContains(response, "home-guide-number", html=False)
        self.assertIn("margin-bottom: 30px;", css)
        self.assertIn("font-size: 1.1rem;", css)
        self.assertIn("font-size: 0.94rem;", css)
        self.assertIn("text-align: center;", css)
        self.assertIn("max-width: none;", css)
        self.assertContains(response, "tannenberg.css?v=16", html=False)

    def test_home_shows_compact_search_guide_and_guest_account_benefits(self):
        response = self.client.get(
            reverse("home"),
            HTTP_HOST="127.0.0.1",
            HTTP_ACCEPT_LANGUAGE="en",
        )

        self.assertContains(response, "Find the right product")
        self.assertContains(response, "ehitusnael 100")
        self.assertContains(response, "scan a barcode with the camera")
        self.assertContains(response, "Get more with a free account")
        self.assertContains(response, "DEPO group purchases")
        self.assertContains(response, 'data-guest-account-guide', html=False)
        self.assertContains(response, reverse("register"))
        self.assertContains(response, reverse("login"))

    def test_home_hides_account_guide_from_authenticated_users(self):
        self.client.force_login(self.create_user("home-guide-user"))

        response = self.client.get(
            reverse("home"),
            HTTP_HOST="127.0.0.1",
            HTTP_ACCEPT_LANGUAGE="en",
        )

        self.assertContains(response, "Find the right product")
        self.assertContains(response, 'class="home-guide is-single"', html=False)
        self.assertNotContains(response, "Get more with a free account")
        self.assertNotContains(response, 'data-guest-account-guide', html=False)

    def test_home_search_includes_accessible_barcode_scanner(self):
        response = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")

        self.assertContains(response, "barcode-scanner.css?v=2", html=False)
        self.assertContains(response, 'data-barcode-scanner-trigger')
        self.assertContains(response, 'aria-label="Сканировать штрихкод камерой"')
        self.assertContains(response, 'data-barcode-scanner-modal')
        self.assertContains(response, 'accept="image/*"')
        self.assertContains(response, 'capture="environment"')

    def test_search_and_catalog_forms_support_barcode_scanner(self):
        search_response = self.client.get(reverse("product_search"), HTTP_HOST="127.0.0.1")
        catalog_response = self.client.get(reverse("catalog"), HTTP_HOST="127.0.0.1")

        for response in (search_response, catalog_response):
            self.assertContains(response, 'data-barcode-search-form')
            self.assertContains(response, 'data-barcode-search-input')
            self.assertContains(response, 'data-barcode-scanner-trigger')

    def test_product_search_page_opens_without_query(self):
        response = self.client.get(reverse("product_search"), HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Введите название, SKU или штрихкод")

    def test_product_search_shows_exact_barcode_and_same_product(self):
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
        self.assertContains(response, "Результаты поиска")
        self.assertContains(response, exact.original_name)
        self.assertContains(response, same.original_name)

    def test_product_search_does_not_expose_internal_ranking_details_to_staff(self):
        offer = self.create_offer(name="Höövelpruss 50x50x3000 mm")
        staff_user = self.create_user("search-staff")
        staff_user.is_staff = True
        staff_user.save(update_fields=["is_staff"])
        self.client.force_login(staff_user)

        response = self.client.get(reverse("product_search"), {"q": "pruss"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, offer.original_name)
        self.assertNotContains(response, "match-debug")
        self.assertNotContains(response, "name tokens overlap")
        self.assertNotContains(response, "query tokens covered")
        self.assertNotContains(response, "compound word")

    def test_product_search_paginates_after_complete_stable_sorting(self):
        offers_by_price = {}
        for index in range(30):
            price = Decimal(30 - index)
            offer = self.create_offer(
                name=f"Pruss product {index:02d}",
                shop=self.other_shop if index % 2 else self.shop,
                category=self.other_category if index % 2 else self.category,
                sku=f"PRUSS-PAGE-{index}",
                barcode=f"PRUSS-EAN-{index}",
                external_id=f"pruss-page-{index}",
                price=price,
            )
            offers_by_price[price] = offer.pk

        expected_ids = [offers_by_price[price] for price in sorted(offers_by_price)]
        first_page = self.client.get(reverse("product_search"), {"q": "pruss"}, HTTP_HOST="127.0.0.1")
        second_page = self.client.get(
            reverse("product_search"),
            {"q": "pruss", "page": 2},
            HTTP_HOST="127.0.0.1",
        )
        repeated_second_page = self.client.get(
            reverse("product_search"),
            {"q": "pruss", "page": 2},
            HTTP_HOST="127.0.0.1",
        )

        first_ids = [match.offer.pk for match in first_page.context["results_page"].object_list]
        second_ids = [match.offer.pk for match in second_page.context["results_page"].object_list]
        repeated_second_ids = [match.offer.pk for match in repeated_second_page.context["results_page"].object_list]

        self.assertEqual(first_ids, expected_ids[:24])
        self.assertEqual(second_ids, expected_ids[24:])
        self.assertEqual(second_ids, repeated_second_ids)
        self.assertFalse(set(first_ids) & set(second_ids))

    def test_registration_requires_email(self):
        response = self.client.post(
            reverse("register"),
            {
                "username": "newuser",
                "password1": "StrongPass123",
                "password2": "StrongPass123",
            },
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertFormError(form, "email", form.fields["email"].error_messages["required"])
        self.assertFalse(get_user_model().objects.filter(username="newuser").exists())

    def test_registration_creates_active_user_without_email_confirmation(self):
        response = self.client.post(
            reverse("register"),
            {
                "username": "newuser",
                "email": "NewUser@example.com",
                "password1": "StrongPass123",
                "password2": "StrongPass123",
            },
            HTTP_HOST="127.0.0.1",
        )

        user = get_user_model().objects.get(username="newuser")
        self.assertRedirects(response, reverse("shopping_list"))
        self.assertEqual(user.email, "newuser@example.com")
        self.assertTrue(user.is_active)
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)
        self.assertEqual(len(mail.outbox), 0)

    def test_email_confirmation_activates_user(self):
        user = get_user_model().objects.create_user(
            username="pending-user",
            email="pending@example.com",
            password="StrongPass123",
            is_active=False,
        )
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = email_verification_token.make_token(user)

        response = self.client.get(
            reverse("confirm_email", kwargs={"uidb64": uid, "token": token}),
            HTTP_HOST="127.0.0.1",
        )

        user.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(user.is_active)
        self.assertContains(response, "Email подтверждён")

    def test_login_uses_django_auth(self):
        self.create_user(username="login-user", password="StrongPass123")

        response = self.client.post(
            reverse("login"),
            {"username": "login-user", "password": "StrongPass123", "next": reverse("shopping_list")},
        )

        self.assertRedirects(response, reverse("shopping_list"))
        self.assertIn("_auth_user_id", self.client.session)

    def test_logout_uses_post_and_redirects_home(self):
        self.client.force_login(self.create_user())

        response = self.client.post(reverse("logout"))

        self.assertRedirects(response, reverse("home"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_my_list_requires_login(self):
        response = self.client.get(reverse("shopping_list"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_guest_cannot_add_item_to_shopping_list(self):
        offer = self.create_offer(name="Makita drill")

        response = self.client.post(reverse("add_to_shopping_list", args=[offer.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
        self.assertFalse(ShoppingListItem.objects.exists())

    def test_authenticated_user_can_add_item_to_shopping_list(self):
        user = self.create_user()
        self.client.force_login(user)
        offer = self.create_offer(name="Makita drill")

        response = self.client.post(reverse("add_to_shopping_list", args=[offer.pk]))

        self.assertRedirects(response, reverse("shopping_list"))
        item = ShoppingListItem.objects.get(shopping_list__user=user)
        self.assertEqual(item.source_offer, offer)
        self.assertEqual(item.product, offer.product)
        self.assertEqual(item.quantity, 1)

    def test_search_quantity_control_is_visible_only_to_authenticated_users(self):
        offer = self.create_offer(name="Search quantity product")

        guest_response = self.client.get(
            reverse("product_search"),
            {"q": "Search quantity product"},
            HTTP_ACCEPT_LANGUAGE="en",
        )

        self.assertNotContains(guest_response, f'id="search-quantity-{offer.pk}"', html=False)

        user = self.create_user("search-quantity-user")
        self.client.force_login(user)
        user_response = self.client.get(
            reverse("product_search"),
            {"q": "Search quantity product"},
            HTTP_ACCEPT_LANGUAGE="en",
        )

        self.assertContains(user_response, f'id="search-quantity-{offer.pk}"', html=False)
        self.assertContains(user_response, 'name="quantity"', html=False)
        self.assertContains(user_response, reverse("add_to_shopping_list", args=[offer.pk]))

    def test_search_adds_requested_quantity_and_updates_group_membership(self):
        user = self.create_user("search-group-quantity-user")
        offer = self.create_offer(
            name="Search group quantity product",
            price=Decimal("10.00"),
            quantity_price=Decimal("8.00"),
            quantity_price_min_quantity=3,
        )
        self.client.force_login(user)
        next_url = f'{reverse("product_search")}?q=Search+group+quantity+product'

        response = self.client.post(
            reverse("add_to_shopping_list", args=[offer.pk]),
            {"quantity": "5", "next": next_url},
        )

        self.assertRedirects(response, next_url)
        item = ShoppingListItem.objects.get(shopping_list__user=user, source_offer=offer)
        membership = GroupPurchaseMember.objects.get(user=user, group__offer=offer)
        self.assertEqual(item.quantity, 5)
        self.assertEqual(membership.quantity, 5)

        search_response = self.client.get(
            next_url,
            HTTP_ACCEPT_LANGUAGE="en",
        )
        self.assertContains(
            search_response,
            reverse("update_shopping_list_item_quantity", args=[item.pk]),
        )
        self.assertContains(search_response, 'value="5"', html=False)

    def test_search_does_not_add_invalid_quantity(self):
        user = self.create_user("search-invalid-quantity-user")
        offer = self.create_offer(name="Invalid search quantity")
        self.client.force_login(user)

        response = self.client.post(
            reverse("add_to_shopping_list", args=[offer.pk]),
            {"quantity": "10000"},
        )

        self.assertRedirects(response, reverse("shopping_list"))
        self.assertFalse(
            ShoppingListItem.objects.filter(shopping_list__user=user, source_offer=offer).exists()
        )

    def test_user_can_update_item_quantity(self):
        user = self.create_user("quantity-owner")
        offer = self.create_offer(name="Quantity product")
        item = add_offer_to_shopping_list(user, offer)
        self.client.force_login(user)

        response = self.client.post(
            reverse("update_shopping_list_item_quantity", args=[item.pk]),
            {"quantity": "4"},
        )

        self.assertRedirects(response, reverse("shopping_list"))
        item.refresh_from_db()
        self.assertEqual(item.quantity, 4)

        list_response = self.client.get(reverse("shopping_list"), HTTP_ACCEPT_LANGUAGE="en")
        self.assertContains(
            list_response,
            reverse("update_shopping_list_item_quantity", args=[item.pk]),
        )
        self.assertContains(list_response, 'name="quantity"', html=False)
        self.assertContains(list_response, 'value="4"', html=False)

        print_response = self.client.get(
            reverse("print_shopping_list", args=[user.shopping_list.share_token]),
            HTTP_ACCEPT_LANGUAGE="en",
        )
        self.assertContains(print_response, "4 pcs.")

    def test_invalid_item_quantity_is_not_saved(self):
        user = self.create_user("invalid-quantity-owner")
        item = add_offer_to_shopping_list(user, self.create_offer(name="Quantity limits"))
        self.client.force_login(user)

        for value in ("0", "10000", "not-a-number"):
            with self.subTest(value=value):
                self.client.post(
                    reverse("update_shopping_list_item_quantity", args=[item.pk]),
                    {"quantity": value},
                )
                item.refresh_from_db()
                self.assertEqual(item.quantity, 1)

    def test_user_cannot_update_another_users_item_quantity(self):
        owner = self.create_user("quantity-private-owner")
        other = self.create_user("quantity-private-other")
        item = add_offer_to_shopping_list(owner, self.create_offer(name="Private quantity"))
        self.client.force_login(other)

        response = self.client.post(
            reverse("update_shopping_list_item_quantity", args=[item.pk]),
            {"quantity": "3"},
        )

        self.assertEqual(response.status_code, 404)
        item.refresh_from_db()
        self.assertEqual(item.quantity, 1)

    def test_navigation_uses_precomputed_shopping_list_count(self):
        user = self.create_user("navigation-count-user")
        offer = self.create_offer(name="Navigation count offer")
        add_offer_to_shopping_list(user, offer)
        self.client.force_login(user)

        response = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")

        self.assertEqual(response.context["shopping_list_item_count"], 1)
        self.assertContains(response, f'{reverse("shopping_list")}">', html=False)
        self.assertContains(response, "(1)")

    def test_duplicate_item_is_not_created(self):
        user = self.create_user()
        self.client.force_login(user)
        offer = self.create_offer(name="Makita drill")

        self.client.post(reverse("add_to_shopping_list", args=[offer.pk]))
        self.client.post(reverse("add_to_shopping_list", args=[offer.pk]))

        self.assertEqual(ShoppingListItem.objects.filter(shopping_list__user=user).count(), 1)

    def test_group_purchase_pages_require_login(self):
        list_response = self.client.get(reverse("group_purchase_list"))
        chat_response = self.client.get(reverse("group_purchase_chat", args=[999]))

        self.assertEqual(list_response.status_code, 302)
        self.assertIn(reverse("login"), list_response["Location"])
        self.assertEqual(chat_response.status_code, 302)
        self.assertIn(reverse("login"), chat_response["Location"])

    def test_regular_offer_does_not_create_group_purchase(self):
        user = self.create_user("regular-offer-user")
        offer = self.create_offer(name="Regular hammer", price=Decimal("10.00"))

        add_offer_to_shopping_list(user, offer)

        self.assertFalse(GroupPurchase.objects.exists())
        self.assertFalse(GroupPurchaseMember.objects.exists())

    def test_quantity_discount_offer_creates_group_and_membership(self):
        user = self.create_user("group-owner")
        offer = self.create_offer(
            name="DEPO group screws",
            price=Decimal("10.00"),
            quantity_price=Decimal("8.50"),
            quantity_price_min_quantity=3,
        )

        item = add_offer_to_shopping_list(user, offer)

        group = GroupPurchase.objects.get(offer=offer, status=GroupPurchase.Status.OPEN)
        membership = GroupPurchaseMember.objects.get(group=group, user=user)
        self.assertEqual(group.target_quantity, 3)
        self.assertEqual(group.quantity_price, Decimal("8.50"))
        self.assertEqual(membership.shopping_list_item, item)
        self.assertEqual(membership.quantity, 1)

    def test_quantity_update_is_reflected_in_group_purchase(self):
        user = self.create_user("group-quantity-owner")
        offer = self.create_offer(
            name="DEPO quantity group product",
            price=Decimal("10.00"),
            quantity_price=Decimal("8.00"),
            quantity_price_min_quantity=3,
        )
        item = add_offer_to_shopping_list(user, offer)
        group = GroupPurchase.objects.get(offer=offer, status=GroupPurchase.Status.OPEN)
        self.client.force_login(user)

        self.client.post(
            reverse("update_shopping_list_item_quantity", args=[item.pk]),
            {"quantity": "4"},
        )

        membership = GroupPurchaseMember.objects.get(group=group, user=user)
        self.assertEqual(membership.quantity, 4)
        response = self.client.get(reverse("group_purchase_list"))
        rendered_group = response.context["page_obj"].object_list[0]
        self.assertEqual(rendered_group.quantity_count, 4)
        self.assertEqual(rendered_group.user_quantity, 4)
        self.assertEqual(rendered_group.regular_total, Decimal("40.00"))
        self.assertEqual(rendered_group.group_total, Decimal("32.00"))

    def test_group_purchase_is_bound_to_exact_store_offer(self):
        first = self.create_user("exact-offer-first")
        second = self.create_user("exact-offer-second")
        shared_barcode = "4740000000777"
        first_offer = self.create_offer(
            name="Same product in ESPAK",
            barcode=shared_barcode,
            external_id="espak-group-offer",
            price=Decimal("10.00"),
            quantity_price=Decimal("8.00"),
            quantity_price_min_quantity=2,
        )
        second_offer = self.create_offer(
            name="Same product in DEPO",
            shop=self.other_shop,
            category=self.other_category,
            sku="DEPO-GROUP-OFFER",
            barcode=shared_barcode,
            external_id="depo-group-offer",
            price=Decimal("9.00"),
            quantity_price=Decimal("7.50"),
            quantity_price_min_quantity=2,
        )

        add_offer_to_shopping_list(first, first_offer)
        add_offer_to_shopping_list(second, second_offer)

        self.assertEqual(GroupPurchase.objects.count(), 2)
        self.assertEqual(GroupPurchase.objects.get(offer=first_offer).members.count(), 1)
        self.assertEqual(GroupPurchase.objects.get(offer=second_offer).members.count(), 1)

    def test_opening_list_syncs_existing_item_that_gained_quantity_discount(self):
        user = self.create_user("late-discount-user")
        offer = self.create_offer(
            name="Late discount product",
            price=Decimal("10.00"),
            quantity_price=None,
            quantity_price_min_quantity=None,
        )
        item = add_offer_to_shopping_list(user, offer)
        ProductOffer.objects.filter(pk=offer.pk).update(
            quantity_price=Decimal("8.00"),
            quantity_price_min_quantity=2,
        )
        offer.refresh_from_db()
        self.assertFalse(GroupPurchase.objects.exists())
        self.client.force_login(user)

        self.client.get(reverse("shopping_list"))

        self.assertTrue(
            GroupPurchaseMember.objects.filter(
                shopping_list_item=item,
                group__offer=offer,
                group__status=GroupPurchase.Status.OPEN,
            ).exists()
        )

    def test_two_users_share_one_group_and_see_chat_in_their_lists(self):
        first = self.create_user("first-group-user")
        second = self.create_user("second-group-user")
        offer = self.create_offer(
            name="DEPO group cleaner",
            price=Decimal("4.73"),
            quantity_price=Decimal("4.21"),
            quantity_price_min_quantity=2,
        )
        add_offer_to_shopping_list(first, offer)
        add_offer_to_shopping_list(second, offer)
        group = GroupPurchase.objects.get(offer=offer, status=GroupPurchase.Status.OPEN)
        self.client.force_login(first)

        response = self.client.get(reverse("shopping_list"))

        self.assertEqual(GroupPurchase.objects.filter(offer=offer, status="open").count(), 1)
        self.assertEqual(group.members.count(), 2)
        self.assertContains(response, reverse("group_purchase_chat", args=[group.pk]))
        self.assertContains(response, "Общий чат · 2")

    def test_group_purchase_list_shows_only_eligible_added_products(self):
        user = self.create_user("group-list-owner")
        group_offer = self.create_offer(
            name="Group product",
            sku="GROUP-SKU",
            barcode="4740000000999",
            external_id="group-product",
            price=Decimal("12.00"),
            quantity_price=Decimal("9.00"),
            quantity_price_min_quantity=4,
        )
        regular_offer = self.create_offer(
            name="Regular product",
            sku="REGULAR-SKU",
            barcode="4740000000888",
            external_id="regular-product",
            price=Decimal("8.00"),
        )
        add_offer_to_shopping_list(user, group_offer)
        add_offer_to_shopping_list(user, regular_offer)
        self.client.force_login(user)

        response = self.client.get(reverse("group_purchase_list"))

        self.assertContains(response, "Group product")
        self.assertContains(response, "GROUP-SKU")
        self.assertContains(response, "4740000000999")
        self.assertNotContains(response, "Regular product")

    def test_joining_group_adds_offer_to_list_and_opens_chat(self):
        owner = self.create_user("group-join-owner")
        participant = self.create_user("group-join-participant")
        offer = self.create_offer(
            name="Joinable product",
            price=Decimal("5.00"),
            quantity_price=Decimal("4.00"),
            quantity_price_min_quantity=2,
        )
        add_offer_to_shopping_list(owner, offer)
        group = GroupPurchase.objects.get(offer=offer, status=GroupPurchase.Status.OPEN)
        self.client.force_login(participant)

        response = self.client.post(reverse("join_group_purchase", args=[group.pk]))

        self.assertRedirects(response, reverse("group_purchase_chat", args=[group.pk]))
        item = ShoppingListItem.objects.get(shopping_list__user=participant, source_offer=offer)
        self.assertTrue(
            GroupPurchaseMember.objects.filter(
                group=group,
                user=participant,
                shopping_list_item=item,
            ).exists()
        )

    def test_group_chat_is_available_only_to_members(self):
        owner = self.create_user("private-chat-owner")
        outsider = self.create_user("private-chat-outsider")
        offer = self.create_offer(
            name="Private chat product",
            price=Decimal("7.00"),
            quantity_price=Decimal("6.00"),
            quantity_price_min_quantity=2,
        )
        add_offer_to_shopping_list(owner, offer)
        group = GroupPurchase.objects.get(offer=offer, status=GroupPurchase.Status.OPEN)
        self.client.force_login(outsider)

        response = self.client.get(reverse("group_purchase_chat", args=[group.pk]))
        messages_response = self.client.get(reverse("group_purchase_messages", args=[group.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(messages_response.status_code, 404)

    def test_member_can_post_escaped_message_and_activity_is_updated(self):
        user = self.create_user("chat-message-user")
        offer = self.create_offer(
            name="Chat message product",
            price=Decimal("9.00"),
            quantity_price=Decimal("7.00"),
            quantity_price_min_quantity=2,
        )
        add_offer_to_shopping_list(user, offer)
        group = GroupPurchase.objects.get(offer=offer, status=GroupPurchase.Status.OPEN)
        old_activity = timezone.now() - timedelta(hours=2)
        GroupPurchase.objects.filter(pk=group.pk).update(last_activity_at=old_activity)
        self.client.force_login(user)

        response = self.client.post(
            reverse("group_purchase_chat", args=[group.pk]),
            {"body": "Встречаемся в 18:00 <script>alert(1)</script>"},
        )

        self.assertRedirects(response, reverse("group_purchase_chat", args=[group.pk]))
        group.refresh_from_db()
        self.assertGreater(group.last_activity_at, old_activity)
        self.assertEqual(GroupPurchaseMessage.objects.filter(group=group).count(), 1)
        chat_page = self.client.get(reverse("group_purchase_chat", args=[group.pk]))
        self.assertContains(chat_page, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(chat_page, "<script>alert(1)</script>")

        messages_response = self.client.get(
            reverse("group_purchase_messages", args=[group.pk]),
            {"after": 0},
        )
        payload = messages_response.json()["messages"][0]
        self.assertEqual(payload["body"], "Встречаемся в 18:00 <script>alert(1)</script>")
        self.assertTrue(payload["is_own"])

    def test_removing_last_group_item_closes_group(self):
        user = self.create_user("last-member")
        offer = self.create_offer(
            name="Last member product",
            price=Decimal("5.00"),
            quantity_price=Decimal("4.00"),
            quantity_price_min_quantity=2,
        )
        item = add_offer_to_shopping_list(user, offer)
        group = GroupPurchase.objects.get(offer=offer, status=GroupPurchase.Status.OPEN)
        self.client.force_login(user)

        self.client.post(reverse("remove_from_shopping_list", args=[item.pk]))

        group.refresh_from_db()
        self.assertEqual(group.status, GroupPurchase.Status.CLOSED)
        self.assertFalse(group.members.exists())

    def test_removing_one_of_two_items_keeps_group_open(self):
        first = self.create_user("remaining-first")
        second = self.create_user("remaining-second")
        offer = self.create_offer(
            name="Remaining group product",
            price=Decimal("5.00"),
            quantity_price=Decimal("4.00"),
            quantity_price_min_quantity=2,
        )
        first_item = add_offer_to_shopping_list(first, offer)
        add_offer_to_shopping_list(second, offer)
        group = GroupPurchase.objects.get(offer=offer, status=GroupPurchase.Status.OPEN)
        self.client.force_login(first)

        self.client.post(reverse("remove_from_shopping_list", args=[first_item.pk]))

        group.refresh_from_db()
        self.assertEqual(group.status, GroupPurchase.Status.OPEN)
        self.assertEqual(group.members.count(), 1)

    def test_inactive_group_expires_after_seven_days(self):
        user = self.create_user("stale-group-owner")
        offer = self.create_offer(
            name="Stale group product",
            price=Decimal("5.00"),
            quantity_price=Decimal("4.00"),
            quantity_price_min_quantity=2,
        )
        add_offer_to_shopping_list(user, offer)
        group = GroupPurchase.objects.get(offer=offer, status=GroupPurchase.Status.OPEN)
        GroupPurchase.objects.filter(pk=group.pk).update(
            last_activity_at=timezone.now() - timedelta(days=8)
        )
        self.client.force_login(user)

        response = self.client.get(reverse("group_purchase_list"))

        group.refresh_from_db()
        self.assertEqual(group.status, GroupPurchase.Status.EXPIRED)
        self.assertNotContains(response, "Stale group product")

    def test_user_does_not_see_another_users_list(self):
        owner = self.create_user("owner")
        other = self.create_user("other")
        offer = self.create_offer(name="Private Makita drill")
        add_offer_to_shopping_list(owner, offer)
        self.client.force_login(other)

        response = self.client.get(reverse("shopping_list"))

        self.assertNotContains(response, "Private Makita drill")

    def test_user_can_delete_only_own_item(self):
        owner = self.create_user("owner")
        other = self.create_user("other")
        offer = self.create_offer(name="Private Makita drill")
        item = add_offer_to_shopping_list(owner, offer)
        self.client.force_login(other)

        response = self.client.post(reverse("remove_from_shopping_list", args=[item.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(ShoppingListItem.objects.filter(pk=item.pk).exists())

        self.client.force_login(owner)
        response = self.client.post(reverse("remove_from_shopping_list", args=[item.pk]))

        self.assertRedirects(response, reverse("shopping_list"))
        self.assertFalse(ShoppingListItem.objects.filter(pk=item.pk).exists())

    def test_shopping_list_shows_source_store_and_cheaper_offer_actions(self):
        user = self.create_user()
        source = self.create_offer(
            name="Makita DDF482Z",
            barcode="4000000000101",
            brand="Makita",
            model="DDF482Z",
            price=Decimal("120.00"),
            product_url="https://espak.example/makita-ddf482z",
        )
        cheaper = self.create_offer(
            name="Akutrell Makita DDF482Z",
            shop=self.other_shop,
            category=self.other_category,
            sku="DEPO-DDF482-LIST",
            barcode="4000000000101",
            external_id="depo-ddf482-list",
            brand="Makita",
            model="DDF482Z",
            price=Decimal("105.00"),
            product_url="https://depo.example/makita-ddf482z",
        )
        item = add_offer_to_shopping_list(user, source)
        self.client.force_login(user)

        response = self.client.get(reverse("shopping_list"))

        self.assertContains(response, "Выбрано в ESPAK")
        self.assertContains(response, "В DEPO дешевле на 15,00 EUR")
        self.assertContains(response, "Открыть в ESPAK")
        self.assertContains(response, "Открыть в DEPO")
        self.assertContains(response, reverse("replace_with_best_offer", args=[item.pk]))
        self.assertContains(response, reverse("remove_from_shopping_list", args=[item.pk]))
        self.assertNotContains(response, reverse("toggle_shopping_list_item", args=[item.pk]))
        self.assertContains(response, reverse("store_click", args=[source.pk]), count=1)
        self.assertContains(response, reverse("store_click", args=[cheaper.pk]), count=1)
        self.assertNotContains(response, "Выбранные товары")

    def test_shopping_list_contains_email_share_copy_and_print_actions(self):
        user = self.create_user()
        offer = self.create_offer(name="Makita drill")
        add_offer_to_shopping_list(user, offer)
        self.client.force_login(user)

        response = self.client.get(reverse("shopping_list"), HTTP_HOST="127.0.0.1")
        shared_path = reverse("shared_shopping_list", args=[user.shopping_list.share_token])
        print_path = reverse("print_shopping_list", args=[user.shopping_list.share_token])

        self.assertContains(response, "mailto:?")
        self.assertContains(response, "data-share-plan")
        self.assertContains(response, "data-copy-plan")
        self.assertContains(response, reverse("clear_shopping_list"))
        self.assertContains(response, f"http://127.0.0.1{shared_path}")
        self.assertContains(response, f"http://127.0.0.1{print_path}")
        self.assertContains(response, "Печать / PDF")

    def test_user_can_enable_and_disable_shopping_list_price_alerts(self):
        user = self.create_user("price-alert-owner")
        user.email = "price-alert-owner@example.com"
        user.save(update_fields=["email"])
        offer = self.create_offer(name="Makita price alert", price=Decimal("10.00"))
        item = add_offer_to_shopping_list(user, offer)
        self.client.force_login(user)

        response = self.client.post(reverse("update_shopping_list_price_alerts"), {"enabled": "1"})

        self.assertRedirects(response, reverse("shopping_list"))
        user.shopping_list.refresh_from_db()
        item.refresh_from_db()
        self.assertTrue(user.shopping_list.price_alerts_enabled)
        self.assertIsNotNone(user.shopping_list.price_alerts_enabled_at)
        self.assertEqual(item.price_alert_source_price, Decimal("10.00"))
        self.assertIsNotNone(item.price_alert_checked_at)
        list_page = self.client.get(reverse("shopping_list"))
        self.assertContains(list_page, "Уведомления об изменении цен")
        self.assertContains(list_page, user.email)
        self.assertContains(list_page, 'role="switch"')
        self.assertContains(list_page, "data-price-alert-form")
        self.assertNotContains(list_page, ">Сохранить</button>")

        self.client.post(reverse("update_shopping_list_price_alerts"), {"enabled": "0"})
        user.shopping_list.refresh_from_db()
        item.refresh_from_db()
        self.assertFalse(user.shopping_list.price_alerts_enabled)
        self.assertIsNone(item.price_alert_checked_at)

    @override_settings(SITE_URL="https://tannenberg.example")
    def test_price_alert_email_reports_price_decrease_and_increase_without_duplicates(self):
        user = self.create_user("price-change-user")
        user.email = "price-change@example.com"
        user.save(update_fields=["email"])
        offer = self.create_offer(name="Tracked drill", price=Decimal("10.00"))
        item = add_offer_to_shopping_list(user, offer)
        set_shopping_list_price_alerts(user.shopping_list, True)

        offer.price = Decimal("8.00")
        offer.save(update_fields=["price"])
        first_result = send_shopping_list_price_alerts()

        self.assertEqual(first_result.emails_sent, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Цена снизилась: 10,00 EUR → 8,00 EUR", mail.outbox[0].body)
        self.assertIn("https://tannenberg.example/my-list/", mail.outbox[0].body)
        send_shopping_list_price_alerts()
        self.assertEqual(len(mail.outbox), 1)

        offer.price = Decimal("11.00")
        offer.save(update_fields=["price"])
        second_result = send_shopping_list_price_alerts()

        self.assertEqual(second_result.emails_sent, 1)
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("Цена выросла: 8,00 EUR → 11,00 EUR", mail.outbox[1].body)
        item.refresh_from_db()
        self.assertEqual(item.price_alert_source_price, Decimal("11.00"))

    def test_price_alert_email_reports_new_cheaper_store(self):
        user = self.create_user("cheaper-store-user")
        user.email = "cheaper-store@example.com"
        user.save(update_fields=["email"])
        source = self.create_offer(
            name="Makita DDF482 drill",
            barcode="4000000000001",
            price=Decimal("10.00"),
        )
        add_offer_to_shopping_list(user, source)
        set_shopping_list_price_alerts(user.shopping_list, True)
        self.create_offer(
            name="Makita DDF482 akutrell",
            shop=self.other_shop,
            category=self.other_category,
            sku="DEPO-DDF482-ALERT",
            barcode="4000000000001",
            external_id="depo-ddf482-alert",
            price=Decimal("7.00"),
        )

        result = send_shopping_list_price_alerts()

        self.assertEqual(result.emails_sent, 1)
        self.assertIn("В DEPO теперь дешевле: 7,00 EUR", mail.outbox[0].body)
        self.assertIn("Экономия: 3,00 EUR", mail.outbox[0].body)

    def test_disabled_price_alerts_do_not_send_email(self):
        user = self.create_user("disabled-alert-user")
        user.email = "disabled-alert@example.com"
        user.save(update_fields=["email"])
        offer = self.create_offer(name="Untracked drill", price=Decimal("10.00"))
        add_offer_to_shopping_list(user, offer)
        offer.price = Decimal("5.00")
        offer.save(update_fields=["price"])

        result = send_shopping_list_price_alerts()

        self.assertEqual(result.lists_checked, 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_failed_price_alert_email_keeps_previous_snapshot_for_retry(self):
        user = self.create_user("retry-alert-user")
        user.email = "retry-alert@example.com"
        user.save(update_fields=["email"])
        offer = self.create_offer(name="Retry tracked drill", price=Decimal("10.00"))
        item = add_offer_to_shopping_list(user, offer)
        set_shopping_list_price_alerts(user.shopping_list, True)
        offer.price = Decimal("6.00")
        offer.save(update_fields=["price"])

        with patch("main.price_alerts.send_mail", side_effect=RuntimeError("SMTP unavailable")):
            result = send_shopping_list_price_alerts()

        item.refresh_from_db()
        self.assertEqual(result.errors_count, 1)
        self.assertEqual(result.emails_sent, 0)
        self.assertEqual(item.price_alert_source_price, Decimal("10.00"))

    def test_price_alert_management_command_outputs_statistics(self):
        output = StringIO()

        call_command("send_shopping_list_price_alerts", stdout=output)

        self.assertIn("lists=0", output.getvalue())
        self.assertIn("emails=0", output.getvalue())

    def test_shopping_list_contains_popular_messenger_share_links(self):
        user = self.create_user()
        offer = self.create_offer(name="Makita drill")
        add_offer_to_shopping_list(user, offer)
        self.client.force_login(user)

        response = self.client.get(reverse("shopping_list"), HTTP_HOST="127.0.0.1")
        shared_path = reverse("shared_shopping_list", args=[user.shopping_list.share_token])
        shared_url = f"http://127.0.0.1{shared_path}"
        links = {
            messenger["name"]: messenger["url"]
            for messenger in response.context["messenger_share_links"]
        }

        self.assertEqual(
            set(links),
            {"WhatsApp", "Telegram", "Messenger", "Viber", "SMS / iMessage"},
        )
        self.assertIn(shared_url, parse_qs(urlparse(links["WhatsApp"]).query)["text"][0])
        self.assertEqual(
            parse_qs(urlparse(links["Telegram"]).query)["url"][0],
            shared_url,
        )
        self.assertEqual(
            parse_qs(urlparse(links["Messenger"]).query)["link"][0],
            shared_url,
        )
        self.assertIn(shared_url, parse_qs(urlparse(links["Viber"]).query)["text"][0])
        self.assertIn(shared_url, parse_qs(urlparse(links["SMS / iMessage"]).query)["body"][0])
        self.assertContains(response, "data-share-menu")
        self.assertContains(response, "Поделиться")
        self.assertNotContains(response, ">Мессенджеры</summary>")

    def test_user_can_clear_entire_own_list(self):
        user = self.create_user("list-owner")
        other = self.create_user("other-owner")
        own_offer = self.create_offer(name="Own item", external_id="own-list-item")
        other_offer = self.create_offer(
            name="Other item",
            external_id="other-list-item",
            sku="OTHER-LIST",
        )
        add_offer_to_shopping_list(user, own_offer)
        add_offer_to_shopping_list(other, other_offer)
        self.client.force_login(user)

        response = self.client.post(reverse("clear_shopping_list"))

        self.assertRedirects(response, reverse("shopping_list"))
        self.assertFalse(ShoppingListItem.objects.filter(shopping_list__user=user).exists())
        self.assertTrue(ShoppingListItem.objects.filter(shopping_list__user=other).exists())
        self.assertTrue(
            ShoppingListEvent.objects.filter(
                user=user,
                offer=own_offer,
                event_type=ShoppingListEvent.EventType.CLEARED,
            ).exists()
        )

    def test_store_click_is_recorded_before_redirect(self):
        offer = self.create_offer(name="Tracked offer")

        response = self.client.get(
            reverse("store_click", args=[offer.pk]),
            HTTP_REFERER="http://127.0.0.1/search/?q=tracked",
            HTTP_HOST="127.0.0.1",
        )

        click = StoreClick.objects.get()
        self.assertRedirects(response, offer.product_url, fetch_redirect_response=False)
        self.assertEqual(click.shop, offer.shop)
        self.assertEqual(click.offer, offer)
        self.assertIn("/search/", click.source_path)

    def test_site_visit_middleware_aggregates_pageviews_per_session(self):
        browser_headers = {
            "HTTP_HOST": "127.0.0.1",
            "HTTP_USER_AGENT": "Mozilla/5.0 Test Browser",
        }
        self.client.get(reverse("home"), **browser_headers)
        self.client.get(reverse("catalog"), **browser_headers)

        visit = DailySiteVisit.objects.get()
        self.assertEqual(visit.pageviews, 2)
        self.assertEqual(visit.first_path, reverse("home"))
        self.assertEqual(visit.last_path, reverse("catalog"))

    def test_site_visit_middleware_ignores_bots_and_health_checks(self):
        self.client.get(
            reverse("home"),
            HTTP_HOST="127.0.0.1",
            HTTP_USER_AGENT="RailwayHealthCheck/1.0",
        )
        self.client.get(
            reverse("home"),
            HTTP_HOST="127.0.0.1",
            HTTP_USER_AGENT="Googlebot/2.1",
        )

        self.assertFalse(DailySiteVisit.objects.exists())

    def test_cookie_less_requests_from_same_browser_share_visitor(self):
        request_headers = {
            "HTTP_HOST": "127.0.0.1",
            "HTTP_USER_AGENT": "Mozilla/5.0 Cookie-less Browser",
            "HTTP_ACCEPT_LANGUAGE": "et-EE,et;q=0.9",
            "REMOTE_ADDR": "203.0.113.10",
        }
        first_client = self.client_class()
        second_client = self.client_class()

        first_client.get(reverse("home"), **request_headers)
        second_client.get(reverse("home"), **request_headers)

        visit = DailySiteVisit.objects.get()
        self.assertEqual(visit.pageviews, 2)

    def test_statistics_are_visible_only_to_staff(self):
        regular_user = self.create_user("regular")
        self.client.force_login(regular_user)

        regular_response = self.client.get(reverse("statistics_dashboard"))
        regular_home = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")

        self.assertEqual(regular_response.status_code, 302)
        self.assertNotContains(regular_home, reverse("statistics_dashboard"))

        staff_user = self.create_user("staff")
        staff_user.is_staff = True
        staff_user.save(update_fields=["is_staff"])
        self.client.force_login(staff_user)
        staff_response = self.client.get(reverse("statistics_dashboard"), HTTP_HOST="127.0.0.1")
        staff_home = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")

        self.assertEqual(staff_response.status_code, 200)
        self.assertTemplateUsed(staff_response, "main/statistics_dashboard.html")
        self.assertContains(staff_home, reverse("statistics_dashboard"))

    def test_shared_shopping_list_is_public_and_read_only(self):
        user = self.create_user("private-share-owner")
        offer = self.create_offer(name="Shared Makita drill")
        item = add_offer_to_shopping_list(user, offer)

        response = self.client.get(
            reverse("shared_shopping_list", args=[user.shopping_list.share_token]),
            HTTP_HOST="127.0.0.1",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Shared Makita drill")
        self.assertNotContains(response, "private-share-owner")
        self.assertNotContains(response, reverse("remove_from_shopping_list", args=[item.pk]))
        self.assertNotContains(response, reverse("toggle_shopping_list_item", args=[item.pk]))
        self.assertNotContains(response, reverse("replace_with_best_offer", args=[item.pk]))
        self.assertNotContains(response, reverse("clear_shopping_list"))
        self.assertNotContains(response, reverse("update_shopping_list_price_alerts"))

    def test_unknown_shared_shopping_list_returns_404(self):
        response = self.client.get(
            reverse("shared_shopping_list", args=[uuid.uuid4()]),
            HTTP_HOST="127.0.0.1",
        )

        self.assertEqual(response.status_code, 404)

    def test_print_shopping_list_contains_required_product_fields(self):
        user = self.create_user()
        offer = self.create_offer(
            name="Printable Makita drill",
            sku="PRINT-SKU-42",
            barcode="4006381333931",
            price=Decimal("42.50"),
            image_url="https://example.com/print-image.jpg",
        )
        add_offer_to_shopping_list(user, offer)

        response = self.client.get(
            reverse("print_shopping_list", args=[user.shopping_list.share_token]),
            HTTP_HOST="127.0.0.1",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Printable Makita drill")
        self.assertContains(response, "PRINT-SKU-42")
        self.assertContains(response, "4006381333931")
        self.assertContains(response, "42,50 EUR")
        self.assertContains(response, "https://example.com/print-image.jpg")
        self.assertContains(response, "Tannenberg")
        self.assertContains(response, "shopping-list-print.css?v=3", html=False)
        self.assertContains(response, "print-shopping-list.js")

    def test_shopping_lists_receive_distinct_share_tokens(self):
        first_user = self.create_user("share-first")
        second_user = self.create_user("share-second")
        add_offer_to_shopping_list(first_user, self.create_offer(name="First shared product"))
        add_offer_to_shopping_list(
            second_user,
            self.create_offer(
                name="Second shared product",
                sku="SHARE-2",
                barcode="SHARE-EAN-2",
                external_id="share-2",
            ),
        )

        self.assertNotEqual(first_user.shopping_list.share_token, second_user.shopping_list.share_token)

    def test_purchase_plan_groups_selected_items_by_source_shop(self):
        user = self.create_user()
        first_espak = self.create_offer(
            name="Makita drill",
            sku="ESPAK-GROUP-1",
            barcode="GROUP-EAN-1",
            external_id="espak-group-1",
            price=Decimal("10.00"),
        )
        second_espak = self.create_offer(
            name="Bosch saw",
            sku="ESPAK-GROUP-2",
            barcode="GROUP-EAN-2",
            external_id="espak-group-2",
            price=Decimal("20.00"),
        )
        depo = self.create_offer(
            name="Garden hose",
            shop=self.other_shop,
            category=self.other_category,
            sku="DEPO-GROUP-1",
            barcode="GROUP-EAN-3",
            external_id="depo-group-1",
            price=Decimal("5.00"),
        )
        for offer in (first_espak, second_espak, depo):
            add_offer_to_shopping_list(user, offer)

        plan = build_purchase_plan(user.shopping_list)
        groups = {group.shop.code: group for group in plan.groups}

        self.assertEqual([group.shop.code for group in plan.groups], ["depo", "espak"])
        self.assertEqual(len(groups["espak"].rows), 2)
        self.assertEqual(groups["espak"].selected_total, Decimal("30.00"))
        self.assertEqual(len(groups["depo"].rows), 1)
        self.assertEqual(groups["depo"].selected_total, Decimal("5.00"))

    def test_purchase_plan_uses_quantity_and_quantity_price(self):
        user = self.create_user("quantity-plan-owner")
        offer = self.create_offer(
            name="Quantity-priced screws",
            price=Decimal("10.00"),
            quantity_price=Decimal("8.00"),
            quantity_price_min_quantity=3,
        )
        item = add_offer_to_shopping_list(user, offer)
        item.quantity = 3
        item.save(update_fields=["quantity"])

        plan = build_purchase_plan(user.shopping_list)
        row = plan.rows[0]

        self.assertEqual(row.source_price, Decimal("8.00"))
        self.assertEqual(row.source_total, Decimal("24.00"))
        self.assertEqual(row.best_total, Decimal("24.00"))
        self.assertEqual(plan.total_source_cost, Decimal("24.00"))
        self.assertEqual(plan.total_best_cost, Decimal("24.00"))
        self.assertEqual(plan.groups[0].selected_total, Decimal("24.00"))

    def test_user_can_toggle_purchased_state(self):
        user = self.create_user()
        item = add_offer_to_shopping_list(user, self.create_offer(name="Makita drill"))
        self.client.force_login(user)

        response = self.client.post(reverse("toggle_shopping_list_item", args=[item.pk]))

        self.assertRedirects(response, reverse("shopping_list"))
        item.refresh_from_db()
        self.assertTrue(item.is_purchased)

        self.client.post(reverse("toggle_shopping_list_item", args=[item.pk]))
        item.refresh_from_db()
        self.assertFalse(item.is_purchased)

    def test_user_cannot_toggle_another_users_item(self):
        owner = self.create_user("purchase-owner")
        other = self.create_user("purchase-other")
        item = add_offer_to_shopping_list(owner, self.create_offer(name="Private drill"))
        self.client.force_login(other)

        response = self.client.post(reverse("toggle_shopping_list_item", args=[item.pk]))

        self.assertEqual(response.status_code, 404)
        item.refresh_from_db()
        self.assertFalse(item.is_purchased)

    def test_purchase_plan_remaining_total_excludes_purchased_items(self):
        user = self.create_user()
        purchased = add_offer_to_shopping_list(
            user,
            self.create_offer(
                name="Purchased drill",
                sku="PURCHASED-1",
                barcode="PURCHASED-EAN-1",
                external_id="purchased-1",
                brand="Makita",
                model="PURCHASED-MODEL",
                price=Decimal("10.00"),
            ),
        )
        add_offer_to_shopping_list(
            user,
            self.create_offer(
                name="Pending saw",
                sku="PENDING-1",
                barcode="PENDING-EAN-1",
                external_id="pending-1",
                brand="Bosch",
                model="PENDING-MODEL",
                price=Decimal("20.00"),
            ),
        )
        purchased.is_purchased = True
        purchased.save(update_fields=["is_purchased"])

        plan = build_purchase_plan(user.shopping_list)

        self.assertEqual(plan.total_best_cost, Decimal("30.00"))
        self.assertEqual(plan.remaining_best_cost, Decimal("20.00"))

    def test_user_can_replace_item_with_current_best_offer(self):
        user = self.create_user()
        source = self.create_offer(
            name="Makita DDF482Z",
            barcode="4000000000102",
            brand="Makita",
            model="DDF482Z",
            price=Decimal("120.00"),
        )
        cheaper = self.create_offer(
            name="Akutrell Makita DDF482Z",
            shop=self.other_shop,
            category=self.other_category,
            sku="DEPO-DDF482-REPLACE",
            barcode="4000000000102",
            external_id="depo-ddf482-replace",
            brand="Makita",
            model="DDF482Z",
            price=Decimal("105.00"),
        )
        item = add_offer_to_shopping_list(user, source)
        self.client.force_login(user)

        response = self.client.post(reverse("replace_with_best_offer", args=[item.pk]))

        self.assertRedirects(response, reverse("shopping_list"))
        item.refresh_from_db()
        self.assertEqual(item.source_offer, cheaper)
        self.assertEqual(item.product, cheaper.product)
        self.assertEqual(item.name, cheaper.original_name)

    def test_user_cannot_replace_another_users_item(self):
        owner = self.create_user("replace-owner")
        other = self.create_user("replace-other")
        source = self.create_offer(
            name="Makita DDF482Z",
            barcode="4000000000103",
            brand="Makita",
            model="DDF482Z",
            price=Decimal("120.00"),
        )
        item = add_offer_to_shopping_list(owner, source)
        self.client.force_login(other)

        response = self.client.post(reverse("replace_with_best_offer", args=[item.pk]))

        self.assertEqual(response.status_code, 404)
        item.refresh_from_db()
        self.assertEqual(item.source_offer, source)

    def test_replacing_with_existing_list_offer_does_not_create_duplicate(self):
        user = self.create_user()
        source = self.create_offer(
            name="Makita DDF482Z",
            barcode="4000000000104",
            brand="Makita",
            model="DDF482Z",
            price=Decimal("120.00"),
        )
        cheaper = self.create_offer(
            name="Akutrell Makita DDF482Z",
            shop=self.other_shop,
            category=self.other_category,
            sku="DEPO-DDF482-EXISTING",
            barcode="4000000000104",
            external_id="depo-ddf482-existing",
            brand="Makita",
            model="DDF482Z",
            price=Decimal("105.00"),
        )
        source_item = add_offer_to_shopping_list(user, source)
        existing_item = add_offer_to_shopping_list(user, cheaper)
        source_item.quantity = 2
        source_item.save(update_fields=["quantity"])
        existing_item.quantity = 3
        existing_item.save(update_fields=["quantity"])
        self.client.force_login(user)

        response = self.client.post(reverse("replace_with_best_offer", args=[source_item.pk]))

        self.assertRedirects(response, reverse("shopping_list"))
        self.assertFalse(ShoppingListItem.objects.filter(pk=source_item.pk).exists())
        existing_item.refresh_from_db()
        self.assertEqual(existing_item.quantity, 5)
        self.assertEqual(ShoppingListItem.objects.filter(shopping_list__user=user).count(), 1)

    def test_catalog_page_opens(self):
        response = self.client.get(reverse("catalog"), HTTP_HOST="127.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Каталог товаров")

    def test_catalog_does_not_render_categories_until_store_is_selected(self):
        self.create_offer(name="ESPAK drill", external_id="espak-category-offer")
        self.create_offer(
            name="DEPO drill",
            shop=self.other_shop,
            category=self.other_category,
            sku="DEPO-CATEGORY",
            barcode="DEPO-CATEGORY-EAN",
            external_id="depo-category-offer",
        )

        response = self.client.get(reverse("catalog"), HTTP_HOST="127.0.0.1")

        self.assertEqual(list(response.context["categories"]), [])
        self.assertContains(response, 'select name="category" disabled', html=False)

    def test_catalog_renders_only_selected_store_categories(self):
        self.create_offer(name="ESPAK drill", external_id="espak-category-offer")
        self.create_offer(
            name="DEPO drill",
            shop=self.other_shop,
            category=self.other_category,
            sku="DEPO-CATEGORY",
            barcode="DEPO-CATEGORY-EAN",
            external_id="depo-category-offer",
        )

        response = self.client.get(
            reverse("catalog"),
            {"shop": self.shop.code},
            HTTP_HOST="127.0.0.1",
        )

        self.assertEqual(list(response.context["categories"]), [self.category])

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

    def test_catalog_decimal_measurement_variants_are_equivalent(self):
        integer = self.create_offer(
            name="Trimmerijõhv Oregon 2mm",
            sku="JÕHV-INTEGER",
            barcode="",
            external_id="johv-integer",
            price=Decimal("3.00"),
        )
        decimal = self.create_offer(
            name="Trimmerijõhv Makita 2.0mm",
            sku="JÕHV-DECIMAL",
            barcode="",
            external_id="johv-decimal",
            price=Decimal("4.00"),
        )
        ProductOffer.objects.filter(pk=decimal.pk).update(
            normalized_name="trimmerijõhv makita 2.0mm",
            search_text="trimmerijõhv makita 2.0mm",
        )

        compact = self.client.get(reverse("catalog"), {"q": "Trimmerijõhv 2mm"}, HTTP_HOST="127.0.0.1")
        dot = self.client.get(reverse("catalog"), {"q": "Trimmerijõhv 2.0mm"}, HTTP_HOST="127.0.0.1")
        comma = self.client.get(reverse("catalog"), {"q": "Trimmerijõhv 2,0 mm"}, HTTP_HOST="127.0.0.1")

        expected = [integer.pk, decimal.pk]
        self.assertEqual(self.catalog_offer_ids(compact), expected)
        self.assertEqual(self.catalog_offer_ids(dot), expected)
        self.assertEqual(self.catalog_offer_ids(comma), expected)

    def test_catalog_default_text_search_sorts_by_effective_price(self):
        expensive = self.create_offer(
            name="Trimmerijõhv Oregon 2mm",
            sku="JÕHV-EXPENSIVE",
            barcode="",
            external_id="johv-expensive",
            price=Decimal("8.00"),
        )
        discounted = self.create_offer(
            name="Varutrimmerijõhv Makita 2mm",
            sku="JÕHV-DISCOUNTED",
            barcode="",
            external_id="johv-discounted",
            price=Decimal("10.00"),
            sale_price=Decimal("2.50"),
        )

        response = self.client.get(reverse("catalog"), {"q": "trimmerijõhv"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(self.catalog_offer_ids(response), [discounted.pk, expensive.pk])

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
        self.assertNotContains(response, "Подробнее")
        self.assertContains(response, "+ В список")
        self.assertContains(response, "В магазин")

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

        self.assertContains(response, "9,99 EUR")
        self.assertContains(response, "12,99 EUR")
        self.assertContains(
            response,
            f'href="{reverse("store_click", args=[ProductOffer.objects.get().pk])}"',
        )
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

    def test_suggestions_decimal_measurement_variants_are_equivalent(self):
        offer = self.create_offer(
            name="Trimmerijõhv Oregon 2.0mm",
            sku="JÕHV-SUGGESTION",
            barcode="",
            external_id="johv-suggestion",
        )
        ProductOffer.objects.filter(pk=offer.pk).update(
            normalized_name="trimmerijõhv oregon 2.0mm",
            search_text="trimmerijõhv oregon 2.0mm",
        )

        compact = self.client.get(
            reverse("search_suggestions"),
            {"q": "Trimmerijõhv 2mm"},
            HTTP_HOST="127.0.0.1",
        )
        decimal = self.client.get(
            reverse("search_suggestions"),
            {"q": "Trimmerijõhv 2,0mm"},
            HTTP_HOST="127.0.0.1",
        )

        self.assertEqual(compact.json()["results"][0]["id"], offer.pk)
        self.assertEqual(decimal.json()["results"][0]["id"], offer.pk)

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
        offer = self.create_offer(
            name="Bosch drill",
            sku="SKU-1",
            barcode="EAN-1",
            sale_price=Decimal("9.99"),
            quantity_price=Decimal("7.49"),
            quantity_price_min_quantity=6,
        )

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
                "quantity_price",
                "quantity_price_min_quantity",
                "currency",
                "image_url",
                "product_url",
                "detail_url",
            },
        )
        self.assertEqual(result["price"], "12.99")
        self.assertEqual(result["sale_price"], "9.99")
        self.assertEqual(result["quantity_price"], "7.49")
        self.assertEqual(result["quantity_price_min_quantity"], 6)
        self.assertEqual(result["detail_url"], reverse("offer_detail", args=[offer.pk]))

    def test_suggestion_detail_url_matches_offer(self):
        offer = self.create_offer(name="Bosch drill")

        response = self.client.get(reverse("search_suggestions"), {"q": "Bosch"}, HTTP_HOST="127.0.0.1")

        self.assertEqual(response.json()["results"][0]["detail_url"], f"/offer/{offer.pk}/")

    def test_best_offer_selects_cheapest_offer_across_same_product(self):
        user = self.create_user()
        source = self.create_offer(name="Makita DDF482Z", barcode="4000000000001", brand="Makita", model="DDF482Z", price=Decimal("120.00"))
        cheapest = self.create_offer(
            name="Akutrell Makita DDF482Z",
            shop=self.other_shop,
            category=self.other_category,
            sku="DEPO-DDF482",
            barcode="4000000000001",
            external_id="depo-ddf482",
            brand="Makita",
            model="DDF482Z",
            price=Decimal("105.00"),
        )
        self.create_offer(
            name="Makita DDF482Z Bauhof",
            shop=Shop.objects.create(name="Bauhof", code="bauhof"),
            category=None,
            sku="BAUHOF-DDF482",
            barcode="4000000000001",
            external_id="bauhof-ddf482",
            brand="Makita",
            model="DDF482Z",
            price=Decimal("115.00"),
        )
        item = add_offer_to_shopping_list(user, source)

        result = get_best_offer(item)

        self.assertEqual(result.best_offer, cheapest)
        self.assertEqual(result.best_price, Decimal("105.00"))
        self.assertEqual(result.price_difference, Decimal("15.00"))

    def test_best_offer_ignores_alternative_from_selected_shop(self):
        user = self.create_user()
        source = self.create_offer(
            name="Makita DDF482Z",
            barcode="4000000000199",
            brand="Makita",
            model="DDF482Z",
            price=Decimal("120.00"),
        )
        same_shop_cheaper = self.create_offer(
            name="Makita DDF482Z kampaania",
            sku="ESPAK-DDF482-CAMPAIGN",
            barcode="4000000000199",
            external_id="espak-ddf482-campaign",
            brand="Makita",
            model="DDF482Z",
            price=Decimal("80.00"),
        )
        item = add_offer_to_shopping_list(user, source)

        result = get_best_offer(item)

        self.assertEqual(result.best_offer, source)
        self.assertEqual(result.potential_saving, Decimal("0.00"))
        self.assertNotIn(same_shop_cheaper, result.other_offers)

    def test_best_offer_compares_matching_measurements_across_distinct_shops(self):
        user = self.create_user()
        fere = Shop.objects.create(name="FERE", code="fere")
        bauhof = Shop.objects.create(name="Bauhof", code="bauhof")
        source = self.create_offer(
            name="Trimmerijõhv Jasper 1.3mm/15m ruudukujuline",
            shop=self.other_shop,
            category=self.other_category,
            sku="160250",
            barcode="2770060166548",
            external_id="depo-trimmer-line",
            brand="",
            model="",
            price=Decimal("0.98"),
        )
        cheapest = self.create_offer(
            name="TRIMMERIJÕHV, ÜMAR 15m 1,3mm",
            shop=fere,
            category=None,
            sku="E62561",
            barcode="4743217007269",
            external_id="fere-trimmer-line",
            brand="",
            model="",
            price=Decimal("0.86"),
        )
        bauhof_cheapest = self.create_offer(
            name="Trimmerijõhv 1,3mm 15m",
            shop=bauhof,
            category=None,
            sku="BAU-TRIMMER-1",
            barcode="",
            external_id="bauhof-trimmer-line-1",
            brand="",
            model="",
            price=Decimal("1.10"),
        )
        self.create_offer(
            name="Trimmerijõhv 15m 1.3mm",
            shop=bauhof,
            category=None,
            sku="BAU-TRIMMER-2",
            barcode="",
            external_id="bauhof-trimmer-line-2",
            brand="",
            model="",
            price=Decimal("1.20"),
        )
        wrong_diameter = self.create_offer(
            name="Trimmerijõhv Jasper 3.0mm/15m ruudukujuline",
            shop=self.other_shop,
            category=self.other_category,
            sku="160257",
            barcode="2770060166586",
            external_id="depo-trimmer-line-3mm",
            brand="",
            model="",
            price=Decimal("4.32"),
        )
        item = add_offer_to_shopping_list(user, source)

        result = get_best_offer(item)

        self.assertEqual(result.best_offer, cheapest)
        self.assertEqual(result.best_price, Decimal("0.86"))
        self.assertEqual(result.potential_saving, Decimal("0.12"))
        self.assertEqual(result.price_difference, Decimal("0.34"))
        self.assertNotIn(wrong_diameter, result.other_offers)
        self.assertEqual(
            [(offer.shop.code, offer.pk) for offer in result.other_offers],
            [("depo", source.pk), ("bauhof", bauhof_cheapest.pk)],
        )

    def test_best_offer_finds_cheaper_trimmer_with_matching_type_and_power(self):
        user = self.create_user()
        handymann = Shop.objects.create(name="Handymann", code="handymann")
        source = self.create_offer(
            name="Elektrimootoriga murutrimmer QT6045 Jasper 350W/25cm",
            shop=self.other_shop,
            category=self.other_category,
            sku="160452",
            barcode="",
            external_id="depo-qt6045",
            brand="",
            model="",
            price=Decimal("19.56"),
        )
        cheaper = self.create_offer(
            name="Murutrimmer Trolla 350W",
            shop=handymann,
            category=None,
            sku="139-131319",
            barcode="",
            external_id="handymann-139-131319",
            brand="",
            model="",
            price=Decimal("14.99"),
        )
        wrong_product = self.create_offer(
            name="Elektriline tikksaag JS-HF55-1001/HF-JS03A-55 Jasper 350W",
            shop=self.other_shop,
            category=self.other_category,
            sku="160453",
            barcode="",
            external_id="depo-jasper-jigsaw",
            brand="",
            model="",
            price=Decimal("13.69"),
        )
        item = add_offer_to_shopping_list(user, source)

        result = get_best_offer(item)

        self.assertEqual(result.best_offer, cheaper)
        self.assertEqual(result.best_price, Decimal("14.99"))
        self.assertEqual(result.potential_saving, Decimal("4.57"))
        self.assertNotIn(wrong_product, result.other_offers)

    def test_best_offer_rejects_cheaper_trimmer_with_conflicting_power(self):
        user = self.create_user()
        handymann = Shop.objects.create(name="Handymann", code="handymann")
        source = self.create_offer(
            name="Elektrimootoriga murutrimmer QT6045 Jasper 350W/25cm",
            shop=self.other_shop,
            category=self.other_category,
            sku="160452",
            barcode="",
            external_id="depo-qt6045",
            brand="",
            model="",
            price=Decimal("19.56"),
        )
        self.create_offer(
            name="Murutrimmer Trolla 500W",
            shop=handymann,
            category=None,
            sku="139-500",
            barcode="",
            external_id="handymann-139-500",
            brand="",
            model="",
            price=Decimal("9.99"),
        )
        item = add_offer_to_shopping_list(user, source)

        result = get_best_offer(item)

        self.assertEqual(result.best_offer, source)
        self.assertEqual(result.best_price, Decimal("19.56"))
        self.assertEqual(result.potential_saving, Decimal("0.00"))

    def test_best_offer_requires_matching_dimensions_and_weight(self):
        user = self.create_user()
        source = self.create_offer(
            name="Ehitusnael 3.1x100mm 1kg",
            sku="NAEL-SOURCE",
            barcode="",
            external_id="nael-source",
            brand="",
            model="",
            price=Decimal("10.00"),
        )
        equivalent = self.create_offer(
            name="Ehitusnaelad 3.1x100mm 1000g",
            shop=self.other_shop,
            category=self.other_category,
            sku="NAEL-EQUAL",
            barcode="",
            external_id="nael-equal",
            brand="",
            model="",
            price=Decimal("8.00"),
        )
        self.create_offer(
            name="Ehitusnaelad 3.1x90mm 1000g",
            shop=self.other_shop,
            category=self.other_category,
            sku="NAEL-WRONG-LENGTH",
            barcode="",
            external_id="nael-wrong-length",
            brand="",
            model="",
            price=Decimal("1.00"),
        )
        self.create_offer(
            name="Ehitusnaelad 3.1x100mm 500g",
            shop=self.other_shop,
            category=self.other_category,
            sku="NAEL-WRONG-WEIGHT",
            barcode="",
            external_id="nael-wrong-weight",
            brand="",
            model="",
            price=Decimal("2.00"),
        )
        item = add_offer_to_shopping_list(user, source)

        result = get_best_offer(item)

        self.assertEqual(result.best_offer, equivalent)
        self.assertEqual(result.best_price, Decimal("8.00"))
        self.assertEqual(result.potential_saving, Decimal("2.00"))

    def test_best_offer_rejects_same_weight_nails_with_different_dimensions(self):
        user = self.create_user()
        bauhof = Shop.objects.create(name="Bauhof", code="bauhof")
        source = self.create_offer(
            name="EHITUSNAEL HJFASTENERS 4,0X100 5KG CA. 496TK PAKIS",
            shop=bauhof,
            category=None,
            sku="BAUHOF-NAEL-4X100",
            barcode="",
            external_id="bauhof-nael-4x100",
            brand="",
            model="",
            price=Decimal("43.99"),
        )
        wrong_dimensions = self.create_offer(
            name="Ehitusnaelad/tsingitud Metalo Prekyba Ø2xL40mm 5kg",
            shop=self.other_shop,
            category=self.other_category,
            sku="DEPO-NAEL-2X40",
            barcode="",
            external_id="depo-nael-2x40",
            brand="",
            model="",
            price=Decimal("16.47"),
        )
        item = add_offer_to_shopping_list(user, source)

        result = get_best_offer(item)

        self.assertEqual(result.best_offer, source)
        self.assertEqual(result.best_price, Decimal("43.99"))
        self.assertEqual(result.potential_saving, Decimal("0.00"))
        self.assertNotIn(wrong_dimensions, result.other_offers)

    def test_best_offer_rejects_nails_without_selected_dimensions(self):
        user = self.create_user()
        bauhof = Shop.objects.create(name="Bauhof", code="bauhof")
        hammerjack = Shop.objects.create(name="Hammerjack", code="hammerjack")
        source = self.create_offer(
            name="EHITUSNAEL HJFASTENERS 4,0X100 5KG CA. 496TK PAKIS",
            shop=bauhof,
            category=None,
            sku="BAUHOF-NAEL-SIZED",
            barcode="",
            external_id="bauhof-nael-sized",
            brand="",
            model="",
            price=Decimal("43.99"),
        )
        unspecified = self.create_offer(
            name="Ehitusnael pinnakatteta",
            shop=hammerjack,
            category=None,
            sku="HAMMERJACK-NAEL-UNSPECIFIED",
            barcode="",
            external_id="hammerjack-nael-unspecified",
            brand="",
            model="",
            price=Decimal("2.22"),
        )
        item = add_offer_to_shopping_list(user, source)

        result = get_best_offer(item)

        self.assertEqual(result.best_offer, source)
        self.assertNotIn(unspecified, result.other_offers)

    def test_best_offer_rejects_trimmer_accessory(self):
        user = self.create_user()
        hammerjack = Shop.objects.create(name="Hammerjack", code="hammerjack")
        source = self.create_offer(
            name="Elektrimootoriga murutrimmer QT6045 Jasper 350W/25cm",
            sku="DEPO-MURUTRIMMER-SOURCE",
            barcode="",
            external_id="depo-murutrimmer-source",
            brand="",
            model="",
            price=Decimal("19.56"),
        )
        shoulder_strap = self.create_offer(
            name="Õlarihm murutrimmeritele",
            shop=hammerjack,
            category=None,
            sku="HAMMERJACK-SHOULDER-STRAP",
            barcode="",
            external_id="hammerjack-shoulder-strap",
            brand="",
            model="",
            price=Decimal("4.05"),
        )
        item = add_offer_to_shopping_list(user, source)

        result = get_best_offer(item)

        self.assertEqual(result.best_offer, source)
        self.assertNotIn(shoulder_strap, result.other_offers)

    def test_best_offer_does_not_replace_trimmer_with_same_model_spool(self):
        user = self.create_user()
        source = self.create_offer(
            name="Elektrimootoriga murutrimmer QT6045 Jasper 350W/25cm",
            sku="DEPO-QT6045-TRIMMER",
            barcode="",
            external_id="depo-qt6045-trimmer",
            brand="Jasper",
            model="QT6045",
            price=Decimal("19.56"),
        )
        compatible_spool = self.create_offer(
            name="Trimmerijõhvi pooliga Jasper QT6045",
            shop=self.other_shop,
            category=self.other_category,
            sku="DEPO-QT6045-SPOOL",
            barcode="",
            external_id="depo-qt6045-spool",
            brand="Jasper",
            model="QT6045",
            price=Decimal("5.96"),
        )
        item = add_offer_to_shopping_list(user, source)

        result = get_best_offer(item)

        self.assertEqual(result.best_offer, source)
        self.assertEqual(result.potential_saving, Decimal("0.00"))
        self.assertNotIn(compatible_spool, result.other_offers)

    def test_best_offer_does_not_replace_wood_cleaner_with_glass_cleaner(self):
        user = self.create_user()
        decora = Shop.objects.create(name="Decora", code="decora")
        source = self.create_offer(
            name="Puhastusvahend puidule Pinotex Terrace&Wood Cleaner 5L",
            shop=decora,
            category=None,
            sku="DECORA-PINOTEX-CLEANER",
            barcode="",
            external_id="decora-pinotex-cleaner",
            brand="Pinotex",
            model="",
            price=Decimal("19.17"),
        )
        glass_cleaner = self.create_offer(
            name="Klaasipuhastusvahend EWOL 5L",
            shop=self.other_shop,
            category=self.other_category,
            sku="DEPO-EWOL-GLASS-CLEANER",
            barcode="",
            external_id="depo-ewol-glass-cleaner",
            brand="",
            model="",
            price=Decimal("3.70"),
        )
        drain_cleaner = self.create_offer(
            name="Kanalisatsioonipuhastusvahend Krots EWOL 5L",
            shop=Shop.objects.create(name="DEPO 2", code="depo-2"),
            category=None,
            sku="DEPO-EWOL-DRAIN-CLEANER",
            barcode="",
            external_id="depo-ewol-drain-cleaner",
            brand="",
            model="",
            price=Decimal("4.73"),
        )
        item = add_offer_to_shopping_list(user, source)

        result = get_best_offer(item)

        self.assertEqual(result.best_offer, source)
        self.assertEqual(result.potential_saving, Decimal("0.00"))
        self.assertNotIn(glass_cleaner, result.other_offers)
        self.assertNotIn(drain_cleaner, result.other_offers)

    def test_purchase_plan_saving_uses_selected_price_instead_of_highest_offer(self):
        user = self.create_user()
        source = self.create_offer(
            name="Makita DDF482Z",
            barcode="4000000000099",
            brand="Makita",
            model="DDF482Z",
            price=Decimal("10.00"),
        )
        cheapest = self.create_offer(
            name="Akutrell Makita DDF482Z",
            shop=self.other_shop,
            category=self.other_category,
            sku="DEPO-DDF482-CHEAP",
            barcode="4000000000099",
            external_id="depo-ddf482-cheap",
            brand="Makita",
            model="DDF482Z",
            price=Decimal("8.00"),
        )
        self.create_offer(
            name="Makita DDF482Z expensive",
            shop=Shop.objects.create(name="Bauhof", code="bauhof"),
            category=None,
            sku="BAUHOF-DDF482-EXPENSIVE",
            barcode="4000000000099",
            external_id="bauhof-ddf482-expensive",
            brand="Makita",
            model="DDF482Z",
            price=Decimal("100.00"),
        )
        add_offer_to_shopping_list(user, source)

        plan = build_purchase_plan(user.shopping_list)

        self.assertEqual(plan.rows[0].best_offer, cheapest)
        self.assertEqual(plan.total_best_cost, Decimal("8.00"))
        self.assertEqual(plan.total_source_cost, Decimal("10.00"))
        self.assertEqual(plan.potential_saving, Decimal("2.00"))

    def test_price_change_is_reflected_without_changing_shopping_list_item(self):
        user = self.create_user()
        source = self.create_offer(name="Makita DDF482Z", barcode="4000000000001", brand="Makita", model="DDF482Z", price=Decimal("120.00"))
        item = add_offer_to_shopping_list(user, source)

        source.price = Decimal("99.00")
        source.save()

        result = get_best_offer(item)

        self.assertEqual(ShoppingListItem.objects.get(pk=item.pk).source_offer, source)
        self.assertEqual(result.best_price, Decimal("99.00"))

    def test_purchase_plan_selects_cheapest_shops_and_counts_saving(self):
        user = self.create_user()
        bauhof = Shop.objects.create(name="Bauhof", code="bauhof")
        first = self.create_offer(name="Makita DDF482Z", barcode="4000000000001", brand="Makita", model="DDF482Z", price=Decimal("120.00"))
        first_cheapest = self.create_offer(
            name="Akutrell Makita DDF482Z",
            shop=self.other_shop,
            category=self.other_category,
            sku="DEPO-DDF482",
            barcode="4000000000001",
            external_id="depo-ddf482",
            brand="Makita",
            model="DDF482Z",
            price=Decimal("100.00"),
        )
        second = self.create_offer(
            name="Bosch GSR 18V",
            sku="BOSCH-1",
            barcode="4000000000002",
            external_id="bosch-1",
            brand="Bosch",
            model="GSR18V",
            price=Decimal("50.00"),
        )
        second_cheapest = self.create_offer(
            name="Bosch GSR 18V Bauhof",
            shop=bauhof,
            category=None,
            sku="BAUHOF-BOSCH",
            barcode="4000000000002",
            external_id="bauhof-bosch",
            brand="Bosch",
            model="GSR18V",
            price=Decimal("40.00"),
        )
        add_offer_to_shopping_list(user, first)
        add_offer_to_shopping_list(user, second)

        plan = build_purchase_plan(user.shopping_list)

        self.assertEqual(
            {row.best_offer.shop for row in plan.rows},
            {first_cheapest.shop, second_cheapest.shop},
        )
        self.assertEqual(plan.total_best_cost, Decimal("140.00"))
        self.assertEqual(plan.total_source_cost, Decimal("170.00"))
        self.assertEqual(plan.potential_saving, Decimal("30.00"))


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class MultilingualInterfaceTests(TestCase):
    def setUp(self):
        shop = Shop.objects.create(name="Eesti Testpood", code="eesti-testpood")
        product = Product.objects.create(name="Murutrimmer Trolle 350W")
        self.offer = ProductOffer.objects.create(
            shop=shop,
            product=product,
            external_id="et-product-1",
            sku="TRIMMER-350",
            original_name="Murutrimmer Trolle 350W",
            price=Decimal("14.99"),
            currency="EUR",
        )

    def select_language(self, language):
        response = self.client.post(
            reverse("set_language"),
            {"language": language, "next": reverse("home")},
        )
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, language)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language

    def test_language_switcher_supports_estonian_russian_and_english(self):
        response = self.client.get(reverse("home"))

        self.assertContains(response, 'name="language"', count=3)
        self.assertContains(response, 'value="et"')
        self.assertContains(response, 'value="ru"')
        self.assertContains(response, 'value="en"')

    def test_home_is_translated_in_all_supported_languages(self):
        expected_headings = {
            "et": "Tooteotsing",
            "ru": "Поиск товаров",
            "en": "Product search",
        }

        for language, heading in expected_headings.items():
            with self.subTest(language=language):
                self.select_language(language)
                response = self.client.get(reverse("home"))
                self.assertContains(response, f'<html lang="{language}">')
                self.assertContains(response, heading)

    def test_home_guide_is_translated_in_all_supported_languages(self):
        expected_guides = {
            "et": "Leia õige toode",
            "ru": "Найдите нужный товар",
            "en": "Find the right product",
        }

        for language, guide_title in expected_guides.items():
            with self.subTest(language=language):
                self.select_language(language)
                response = self.client.get(reverse("home"))
                self.assertContains(response, guide_title)

    def test_product_name_stays_estonian_in_every_interface_language(self):
        translated_store_labels = {
            "et": "Kauplus",
            "ru": "Магазин",
            "en": "Store",
        }

        for language, store_label in translated_store_labels.items():
            with self.subTest(language=language):
                self.select_language(language)
                response = self.client.get(reverse("offer_detail", args=[self.offer.pk]))
                self.assertContains(response, "Murutrimmer Trolle 350W")
                self.assertContains(response, store_label)


@override_settings(
    SITE_URL="https://tannenberg.example",
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class SeoTests(TestCase):
    def setUp(self):
        cache.clear()
        self.first_shop = Shop.objects.create(name="DEPO", code="depo")
        self.second_shop = Shop.objects.create(name="Bauhof", code="bauhof")
        self.first_category = Category.objects.create(
            shop=self.first_shop,
            external_id="drills",
            name="Akutrellid",
        )
        self.second_category = Category.objects.create(
            shop=self.second_shop,
            external_id="cordless-drills",
            name="Akutrellid ja kruvikeerajad",
        )

    def create_offer(
        self,
        *,
        shop=None,
        category=None,
        external_id="seo-offer-1",
        barcode="4740000000001",
        price=Decimal("100.00"),
        sale_price=None,
        name="Akutrell Makita DDF482Z",
    ):
        shop = shop or self.first_shop
        if category is None:
            category = self.first_category if shop == self.first_shop else self.second_category
        product = Product.objects.create(
            name=name,
            brand="Makita",
            model="DDF482Z",
            barcode=barcode,
        )
        return ProductOffer.objects.create(
            shop=shop,
            product=product,
            category=category,
            external_id=external_id,
            sku=external_id.upper(),
            barcode=barcode,
            original_name=name,
            description="18 V cordless drill",
            price=price,
            sale_price=sale_price,
            currency="EUR",
            product_url=f"https://{shop.code}.example/{external_id}",
            image_url=f"https://{shop.code}.example/{external_id}.jpg",
        )

    def create_comparison(self):
        expensive = self.create_offer(price=Decimal("120.00"))
        cheaper = self.create_offer(
            shop=self.second_shop,
            external_id="seo-offer-2",
            price=Decimal("110.00"),
            sale_price=Decimal("89.00"),
            name="Makita akutrell DDF482Z",
        )
        return expensive, cheaper

    def test_barcode_comparison_page_uses_exact_ean_and_orders_by_price(self):
        expensive, cheaper = self.create_comparison()
        unrelated = self.create_offer(
            external_id="different-ean",
            barcode="4740000000002",
            price=Decimal("1.00"),
        )

        response = self.client.get(
            reverse("barcode_product_detail", args=[expensive.barcode]),
            HTTP_HOST="testserver",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([offer.pk for offer in response.context["offers"]], [cheaper.pk, expensive.pk])
        self.assertNotContains(response, unrelated.original_name)
        self.assertContains(response, "89.00")
        self.assertContains(response, expensive.barcode)
        self.assertEqual(
            response.context["seo_canonical_url"],
            f"https://tannenberg.example/product/ean/{expensive.barcode}/",
        )

    def test_barcode_comparison_schema_contains_aggregate_offer(self):
        expensive, _cheaper = self.create_comparison()

        response = self.client.get(reverse("barcode_product_detail", args=[expensive.barcode]))
        schema = json.loads(str(response.context["seo_json_ld"]))
        product = next(item for item in schema["@graph"] if item["@type"] == "Product")

        self.assertEqual(product["gtin13"], expensive.barcode)
        self.assertEqual(product["offers"]["@type"], "AggregateOffer")
        self.assertEqual(product["offers"]["lowPrice"], "89.00")
        self.assertEqual(product["offers"]["highPrice"], "120.00")
        self.assertEqual(product["offers"]["offerCount"], 2)

    def test_barcode_comparison_requires_valid_ean_and_two_stores(self):
        single = self.create_offer()

        invalid_response = self.client.get(reverse("barcode_product_detail", args=["EAN-1"]))
        single_response = self.client.get(
            reverse("barcode_product_detail", args=[single.barcode])
        )

        self.assertEqual(invalid_response.status_code, 404)
        self.assertEqual(single_response.status_code, 404)

    def test_offer_detail_canonicalizes_duplicate_ean_to_comparison(self):
        expensive, _cheaper = self.create_comparison()

        response = self.client.get(reverse("offer_detail", args=[expensive.pk]))

        comparison_url = f"https://tannenberg.example/product/ean/{expensive.barcode}/"
        self.assertEqual(response.context["seo_canonical_url"], comparison_url)
        self.assertEqual(response.context["seo_robots"], "noindex,follow")
        self.assertContains(response, f'<link rel="canonical" href="{comparison_url}">')
        self.assertContains(response, 'class="comparison-link"')

    def test_unique_offer_has_self_canonical_and_product_schema(self):
        offer = self.create_offer()

        response = self.client.get(reverse("offer_detail", args=[offer.pk]))
        schema = json.loads(str(response.context["seo_json_ld"]))

        self.assertEqual(
            response.context["seo_canonical_url"],
            f"https://tannenberg.example/offer/{offer.pk}/",
        )
        self.assertEqual(schema["@type"], "Product")
        self.assertEqual(schema["offers"]["price"], "100.00")

    def test_category_page_has_stable_url_and_canonical(self):
        offer = self.create_offer()
        url = reverse(
            "category_catalog",
            args=[self.first_shop.code, self.first_category.pk],
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.first_category.name)
        self.assertContains(response, offer.original_name)
        self.assertEqual(response.context["seo_robots"], "index,follow")
        self.assertEqual(response.context["seo_canonical_url"], f"https://tannenberg.example{url}")

    def test_filtered_catalog_and_search_are_noindex(self):
        self.create_offer()

        catalog_response = self.client.get(reverse("catalog"), {"shop": "depo"})
        search_response = self.client.get(reverse("product_search"), {"q": "makita"})

        self.assertEqual(catalog_response.context["seo_robots"], "noindex,follow")
        self.assertContains(catalog_response, 'content="noindex,follow"')
        self.assertEqual(search_response["X-Robots-Tag"], "noindex, nofollow")
        self.assertContains(search_response, 'content="noindex,nofollow"')

    def test_robots_lists_sitemap_and_private_paths(self):
        response = self.client.get(reverse("robots_txt"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/plain"))
        self.assertContains(response, "Disallow: /my-list/")
        self.assertContains(response, "Sitemap: https://tannenberg.example/sitemap.xml")

    def test_sitemap_index_contains_all_sections(self):
        self.create_comparison()

        response = self.client.get(reverse("sitemap-index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/sitemap-static.xml")
        self.assertContains(response, "/sitemap-products.xml")
        self.assertContains(response, "/sitemap-categories.xml")

    @override_settings(SITE_URL="", ALLOWED_HOSTS=["tannenberg.example"])
    def test_sitemap_uses_forwarded_https_in_production(self):
        response = self.client.get(
            reverse("sitemap-index"),
            HTTP_HOST="tannenberg.example",
            HTTP_X_FORWARDED_PROTO="https",
        )

        self.assertContains(response, "https://tannenberg.example/sitemap-static.xml")

    def test_product_sitemap_only_contains_valid_multi_store_barcodes(self):
        valid, _other = self.create_comparison()
        self.create_offer(
            external_id="single-store",
            barcode="4740000000002",
        )
        self.create_offer(
            shop=self.second_shop,
            external_id="invalid-barcode",
            barcode="EAN-INVALID",
        )

        items = list(BarcodeComparisonSitemap().items())
        response = self.client.get(
            reverse("django.contrib.sitemaps.views.sitemap", args=["products"])
        )

        self.assertEqual(items, [valid.barcode])
        self.assertContains(response, reverse("barcode_product_detail", args=[valid.barcode]))
        self.assertNotContains(response, "4740000000002")
        self.assertNotContains(response, "EAN-INVALID")

    def test_home_carousel_links_to_barcode_comparison_page(self):
        expensive, _cheaper = self.create_comparison()

        response = self.client.get(reverse("home"))

        self.assertContains(
            response,
            reverse("barcode_product_detail", args=[expensive.barcode]),
        )
