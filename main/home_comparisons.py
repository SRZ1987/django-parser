from django.core.cache import cache
from django.db.models import Count, Q

from catalog.models import ProductOffer


COMPARISON_CACHE_KEY = "home-price-comparisons:v2"
COMPARISON_CACHE_SECONDS = 30 * 60
BARCODE_GROUP_LIMIT = 12
GROUP_CANDIDATE_LIMIT = 30


def get_home_price_comparisons():
    cached = cache.get(COMPARISON_CACHE_KEY)
    if cached is not None:
        return cached

    comparisons = _barcode_comparison_groups()
    cache.set(COMPARISON_CACHE_KEY, comparisons, COMPARISON_CACHE_SECONDS)
    return comparisons


def _base_offers():
    return ProductOffer.objects.filter(
        is_active=True,
        is_available=True,
        shop__is_active=True,
    ).filter(Q(price__isnull=False) | Q(sale_price__isnull=False))


def _barcode_comparison_groups():
    barcode_keys = list(
        _base_offers()
        .exclude(barcode="")
        .order_by()
        .values("barcode")
        .annotate(shop_count=Count("shop_id", distinct=True))
        .filter(shop_count__gte=2)
        .order_by("barcode")
        .values_list("barcode", flat=True)[:GROUP_CANDIDATE_LIMIT]
    )
    if not barcode_keys:
        return []

    offers = list(
        _base_offers()
        .filter(barcode__in=barcode_keys)
        .select_related("shop")
        .order_by("barcode", "shop__name", "id")
    )
    offers_by_barcode = {}
    for offer in offers:
        offers_by_barcode.setdefault(offer.barcode, []).append(offer)

    groups = []
    for barcode in barcode_keys:
        group = _serialize_group(offers_by_barcode.get(barcode, []))
        if group:
            groups.append(group)
        if len(groups) >= BARCODE_GROUP_LIMIT:
            break
    return groups


def _serialize_group(offers):
    cheapest_by_shop = {}
    for offer in offers:
        price = _effective_price(offer)
        if price is None:
            continue
        current = cheapest_by_shop.get(offer.shop_id)
        if current is None or price < current[0] or (price == current[0] and offer.pk < current[1].pk):
            cheapest_by_shop[offer.shop_id] = (price, offer)

    ranked = sorted(
        cheapest_by_shop.values(),
        key=lambda item: (item[0], item[1].shop.name.casefold(), item[1].pk),
    )
    if len(ranked) < 2:
        return None

    representative = next((offer for _price, offer in ranked if offer.image_url), ranked[0][1])
    return {
        "name": representative.original_name,
        "image_url": representative.image_url,
        "detail_offer_id": representative.pk,
        "match_type": "barcode",
        "offers": [
            {
                "id": offer.pk,
                "shop": offer.shop.name,
                "price": price,
                "currency": offer.currency or "EUR",
                "product_url": offer.product_url,
                "is_cheapest": index == 0,
            }
            for index, (price, offer) in enumerate(ranked[:5])
        ],
    }


def _effective_price(offer):
    price = offer.price
    sale_price = offer.sale_price
    if sale_price is not None and (price is None or sale_price < price):
        return sale_price
    return price
