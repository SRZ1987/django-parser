import uuid
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from catalog.models import Category, Product, ProductOffer, Shop
from main.email_verification import email_verification_token
from main.models import DailySiteVisit, ShoppingListEvent, ShoppingListItem, StoreClick
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

    def test_home_search_includes_accessible_barcode_scanner(self):
        response = self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")

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
        self.assertFormError(response.context["form"], "email", "This field is required.")
        self.assertFalse(get_user_model().objects.filter(username="newuser").exists())

    def test_registration_creates_inactive_user_and_sends_confirmation(self):
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
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "registration/email_confirmation_sent.html")
        self.assertEqual(user.email, "newuser@example.com")
        self.assertFalse(user.is_active)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/accounts/confirm-email/", mail.outbox[0].body)

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

    def test_duplicate_item_is_not_created(self):
        user = self.create_user()
        self.client.force_login(user)
        offer = self.create_offer(name="Makita drill")

        self.client.post(reverse("add_to_shopping_list", args=[offer.pk]))
        self.client.post(reverse("add_to_shopping_list", args=[offer.pk]))

        self.assertEqual(ShoppingListItem.objects.filter(shopping_list__user=user).count(), 1)

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
        self.assertContains(response, "В DEPO дешевле на 15.00 EUR")
        self.assertContains(response, "Открыть в ESPAK")
        self.assertContains(response, "Открыть в DEPO")
        self.assertContains(response, reverse("replace_with_best_offer", args=[item.pk]))
        self.assertContains(response, reverse("remove_from_shopping_list", args=[item.pk]))
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
        self.assertContains(response, "Мессенджеры")

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
        self.client.get(reverse("home"), HTTP_HOST="127.0.0.1")
        self.client.get(reverse("catalog"), HTTP_HOST="127.0.0.1")

        visit = DailySiteVisit.objects.get()
        self.assertEqual(visit.pageviews, 2)
        self.assertEqual(visit.first_path, reverse("home"))
        self.assertEqual(visit.last_path, reverse("catalog"))

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
        self.assertContains(response, "42.50 EUR")
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
        self.client.force_login(user)

        response = self.client.post(reverse("replace_with_best_offer", args=[source_item.pk]))

        self.assertRedirects(response, reverse("shopping_list"))
        self.assertFalse(ShoppingListItem.objects.filter(pk=source_item.pk).exists())
        self.assertTrue(ShoppingListItem.objects.filter(pk=existing_item.pk).exists())
        self.assertEqual(ShoppingListItem.objects.filter(shopping_list__user=user).count(), 1)

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
