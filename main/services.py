from dataclasses import dataclass, field
from decimal import Decimal

from catalog.models import ProductOffer
from catalog.services.product_matching import offers_are_comparable
from catalog.services.product_search import find_matches

from .models import ShoppingList, ShoppingListItem


@dataclass(frozen=True)
class BestOfferResult:
    item: ShoppingListItem
    best_offer: ProductOffer | None
    other_offers: list[ProductOffer] = field(default_factory=list)
    best_price: Decimal | None = None
    source_price: Decimal | None = None
    highest_price: Decimal | None = None
    price_difference: Decimal | None = None
    potential_saving: Decimal = Decimal("0.00")


@dataclass(frozen=True)
class PurchasePlan:
    rows: list[BestOfferResult]
    grouped_by_shop: dict
    total_best_cost: Decimal
    total_source_cost: Decimal
    potential_saving: Decimal


def get_or_create_shopping_list(user):
    shopping_list, _created = ShoppingList.objects.get_or_create(user=user)
    return shopping_list


def add_offer_to_shopping_list(user, offer):
    shopping_list = get_or_create_shopping_list(user)
    item, _created = ShoppingListItem.objects.get_or_create(
        shopping_list=shopping_list,
        source_offer=offer,
        defaults={
            "product": offer.product,
            "name": offer.original_name,
        },
    )
    return item


def get_best_offer(item):
    matches = find_matches(item.source_offer)
    candidate_matches = matches.exact_matches + matches.same_product + matches.similar_products
    offers = [
        match.offer
        for match in candidate_matches
        if match.offer.current_price is not None
        and offers_are_comparable(item.source_offer, match.offer)
    ]

    if item.source_offer.current_price is not None and item.source_offer not in offers:
        offers.append(item.source_offer)

    if not offers:
        return BestOfferResult(item=item, best_offer=None)

    offers = sorted(offers, key=lambda offer: (offer.current_price, offer.shop.name, offer.original_name))
    best_offer = offers[0]
    highest_price = max(offer.current_price for offer in offers)
    best_price = best_offer.current_price
    source_price = item.source_offer.current_price
    potential_saving = max(Decimal("0.00"), (source_price or best_price) - best_price)
    other_offers_by_shop = {}
    for offer in offers[1:]:
        if offer.shop_id != best_offer.shop_id:
            other_offers_by_shop.setdefault(offer.shop_id, offer)

    return BestOfferResult(
        item=item,
        best_offer=best_offer,
        other_offers=list(other_offers_by_shop.values()),
        best_price=best_price,
        source_price=source_price,
        highest_price=highest_price,
        price_difference=highest_price - best_price,
        potential_saving=potential_saving,
    )


def build_purchase_plan(shopping_list):
    items = (
        shopping_list.items.select_related(
            "product",
            "source_offer",
            "source_offer__shop",
            "source_offer__category",
            "source_offer__product",
        )
        .order_by("name", "id")
    )
    rows = [get_best_offer(item) for item in items]
    grouped_by_shop = {}
    total_best_cost = Decimal("0.00")
    total_source_cost = Decimal("0.00")

    for row in rows:
        if row.best_offer is None or row.best_price is None:
            continue
        total_best_cost += row.best_price
        total_source_cost += row.source_price or row.best_price
        grouped_by_shop.setdefault(row.best_offer.shop, []).append(row)

    return PurchasePlan(
        rows=rows,
        grouped_by_shop=grouped_by_shop,
        total_best_cost=total_best_cost,
        total_source_cost=total_source_cost,
        potential_saving=sum((row.potential_saving for row in rows), Decimal("0.00")),
    )
