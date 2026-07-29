from django.core.paginator import Paginator
from django.db.models import Case, IntegerField, Q, Value, When
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from catalog.models import Category, ProductOffer, Shop


CATALOG_PAGE_SIZE = 24
CATALOG_SORT_OPTIONS = {
    "relevance",
    "name_asc",
    "name_desc",
    "price_asc",
    "price_desc",
    "newest",
}


def product_offer_search_query(query):
    return (
        Q(original_name__icontains=query)
        | Q(sku__icontains=query)
        | Q(barcode__icontains=query)
        | Q(external_id__icontains=query)
        | Q(product__name__icontains=query)
        | Q(product__brand__icontains=query)
        | Q(product__model__icontains=query)
    )


def available_offers():
    return ProductOffer.objects.filter(is_active=True, is_available=True)


def home(request):
    query = request.GET.get("q", "").strip()

    return render(
        request,
        "main/home.html",
        {
            "query": query,
        },
    )


def active_categories(shop=None):
    categories = Category.objects.filter(
        offers__is_active=True,
        offers__is_available=True,
    )
    if shop:
        categories = categories.filter(shop=shop)
    return categories.distinct().order_by("name")


def apply_catalog_sorting(offers, sort, query):
    if sort == "name_desc":
        return offers.order_by("-original_name", "id")

    if sort == "price_asc":
        return (
            offers.annotate(
                effective_price=Coalesce("sale_price", "price"),
                price_missing=Case(
                    When(sale_price__isnull=True, price__isnull=True, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                ),
            )
            .order_by("price_missing", "effective_price", "id")
        )

    if sort == "price_desc":
        return (
            offers.annotate(
                effective_price=Coalesce("sale_price", "price"),
                price_missing=Case(
                    When(sale_price__isnull=True, price__isnull=True, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                ),
            )
            .order_by("price_missing", "-effective_price", "id")
        )

    if sort == "newest":
        return offers.order_by("-created_at", "-id")

    if sort == "relevance" and query:
        return (
            offers.annotate(
                exact_identifier_match=Case(
                    When(
                        Q(sku__iexact=query) | Q(barcode__iexact=query) | Q(external_id__iexact=query),
                        then=Value(0),
                    ),
                    default=Value(1),
                    output_field=IntegerField(),
                )
            )
            .order_by("exact_identifier_match", "original_name", "id")
        )

    return offers.order_by("original_name", "id")


def catalog_view(request):
    query = request.GET.get("q", "").strip()
    shop_code = request.GET.get("shop", "").strip()
    category_id = request.GET.get("category", "").strip()
    sort = request.GET.get("sort", "relevance").strip()
    if sort not in CATALOG_SORT_OPTIONS:
        sort = "relevance"

    shops = Shop.objects.filter(is_active=True).order_by("name")
    selected_shop = shops.filter(code=shop_code).first() if shop_code else None

    offers = available_offers().select_related("shop", "category", "product")

    if query:
        offers = offers.filter(product_offer_search_query(query))

    if selected_shop:
        offers = offers.filter(shop=selected_shop)

    categories = active_categories(selected_shop)
    selected_category = None
    if category_id.isdigit():
        selected_category = categories.filter(pk=int(category_id)).first()
        if selected_category:
            offers = offers.filter(category=selected_category)

    offers = apply_catalog_sorting(offers, sort, query)

    paginator = Paginator(offers, CATALOG_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    page_params = request.GET.copy()
    page_params.pop("page", None)

    return render(
        request,
        "main/catalog.html",
        {
            "query": query,
            "shops": shops,
            "categories": categories,
            "selected_shop_code": selected_shop.code if selected_shop else "",
            "selected_category_id": str(selected_category.pk) if selected_category else "",
            "selected_sort": sort,
            "sort_options": [
                ("relevance", "По релевантности"),
                ("name_asc", "Название: А-Я"),
                ("name_desc", "Название: Я-А"),
                ("price_asc", "Сначала дешевле"),
                ("price_desc", "Сначала дороже"),
                ("newest", "Сначала новые"),
            ],
            "page_obj": page_obj,
            "page_params": page_params.urlencode(),
            "page_range": paginator.get_elided_page_range(page_obj.number),
        },
    )


def search_suggestions(request):
    query = request.GET.get("q", "").strip()

    if len(query) < 2:
        return JsonResponse({"results": []})

    offers = (
        available_offers()
        .filter(product_offer_search_query(query))
        .select_related("shop", "category", "product")
        .order_by("original_name")[:8]
    )

    return JsonResponse(
        {
            "results": [
                {
                    "id": offer.id,
                    "name": offer.original_name,
                    "shop": offer.shop.name,
                    "category": offer.category.name if offer.category else "",
                    "sku": offer.sku,
                    "barcode": offer.barcode,
                    "price": str(offer.price) if offer.price is not None else None,
                    "sale_price": str(offer.sale_price) if offer.sale_price is not None else None,
                    "currency": offer.currency,
                    "image_url": offer.image_url,
                    "product_url": offer.product_url,
                    "detail_url": reverse("offer_detail", args=[offer.pk]),
                }
                for offer in offers
            ]
        }
    )


def offer_detail(request, pk):
    offer = get_object_or_404(
        available_offers().select_related("shop", "category", "product"),
        pk=pk,
    )
    return render(request, "main/offer_detail.html", {"offer": offer})
