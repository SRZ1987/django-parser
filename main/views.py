from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from catalog.models import ProductOffer


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
    offers = ProductOffer.objects.none()

    if query:
        offers = (
            available_offers()
            .filter(product_offer_search_query(query))
            .select_related("shop", "category", "product")
            .order_by("original_name")[:50]
        )

    return render(
        request,
        "main/home.html",
        {
            "query": query,
            "offers": offers,
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
