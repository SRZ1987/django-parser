import logging

from django.core.cache import cache
from django.db import DatabaseError, connection
from django.db.models import CharField, Count, F, Func, Q

from catalog.models import ProductOffer
from catalog.services.normalization import tokenize


COMPARISON_CACHE_KEY = "home-price-comparisons:v1"
COMPARISON_CACHE_SECONDS = 30 * 60
BARCODE_GROUP_LIMIT = 6
NAME_GROUP_LIMIT = 4
GROUP_CANDIDATE_LIMIT = 30
logger = logging.getLogger(__name__)


class ProductNameSignature(Func):
    function = "catalog_product_name_signature"
    output_field = CharField(max_length=32)


def get_home_price_comparisons():
    cached = cache.get(COMPARISON_CACHE_KEY)
    if cached is not None:
        return cached

    comparisons = _build_home_price_comparisons()
    cache.set(COMPARISON_CACHE_KEY, comparisons, COMPARISON_CACHE_SECONDS)
    return comparisons


def _build_home_price_comparisons():
    barcode_groups = _barcode_comparison_groups()
    name_groups = []
    if NAME_GROUP_LIMIT:
        try:
            name_groups = _name_comparison_groups()
        except DatabaseError:
            # A deploy can briefly serve code before its concurrent index migration
            # completes. Barcode comparisons remain available in that window.
            logger.exception("Could not build homepage name comparison groups")
            name_groups = []

    comparisons = []
    seen_offer_sets = set()
    for group in [*barcode_groups, *name_groups]:
        offer_set = frozenset(offer["id"] for offer in group["offers"])
        if offer_set in seen_offer_sets:
            continue
        seen_offer_sets.add(offer_set)
        comparisons.append(group)
        if len(comparisons) >= BARCODE_GROUP_LIMIT + NAME_GROUP_LIMIT:
            break
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
        group = _serialize_group(offers_by_barcode.get(barcode, []), match_type="barcode")
        if group:
            groups.append(group)
        if len(groups) >= BARCODE_GROUP_LIMIT:
            break
    return groups


def _name_comparison_groups():
    if connection.vendor == "postgresql":
        return _postgresql_name_comparison_groups()
    return _python_name_comparison_groups()


def _postgresql_name_comparison_groups():
    signed = _base_offers().exclude(normalized_name="").annotate(
        name_signature=ProductNameSignature(F("normalized_name"))
    )
    signature_keys = list(
        signed.order_by()
        .values("name_signature")
        .annotate(shop_count=Count("shop_id", distinct=True))
        .filter(shop_count__gte=2)
        .order_by("name_signature")
        .values_list("name_signature", flat=True)[:GROUP_CANDIDATE_LIMIT]
    )
    if not signature_keys:
        return []

    offers = list(
        signed.filter(name_signature__in=signature_keys)
        .select_related("shop")
        .order_by("shop__name", "id")
    )
    offers_by_signature = {}
    for offer in offers:
        signature = _canonical_name(offer.normalized_name)
        offers_by_signature.setdefault(signature, []).append(offer)

    return _serialize_name_groups(offers_by_signature)


def _python_name_comparison_groups():
    offers = list(
        _base_offers()
        .exclude(normalized_name="")
        .select_related("shop")
        .order_by("id")[:5000]
    )
    offers_by_signature = {}
    for offer in offers:
        offers_by_signature.setdefault(_canonical_name(offer.normalized_name), []).append(offer)
    return _serialize_name_groups(offers_by_signature)


def _serialize_name_groups(offers_by_signature):
    groups = []
    for signature in sorted(offers_by_signature):
        group = _serialize_group(offers_by_signature[signature], match_type="name")
        if group:
            groups.append(group)
        if len(groups) >= NAME_GROUP_LIMIT:
            break
    return groups


def _serialize_group(offers, *, match_type):
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
        "match_type": match_type,
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


def _canonical_name(value):
    return " ".join(sorted(tokenize(value)))
