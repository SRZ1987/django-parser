from django.db.models import Q
from django.shortcuts import render

from catalog.models import ProductOffer


def home(request):
    query = request.GET.get("q", "").strip()
    offers = ProductOffer.objects.none()

    if query:
        search_query = (
            Q(original_name__icontains=query)
            | Q(sku__icontains=query)
            | Q(barcode__icontains=query)
            | Q(external_id__icontains=query)
            | Q(product__name__icontains=query)
            | Q(product__brand__icontains=query)
            | Q(product__model__icontains=query)
        )
        offers = (
            ProductOffer.objects.filter(is_active=True, is_available=True)
            .filter(search_query)
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
