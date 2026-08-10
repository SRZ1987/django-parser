from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction

from catalog.models import ProductOffer, Shop
from catalog.services.product_matching import offers_are_comparable
from catalog.services.product_search import find_matches

from .models import ShoppingList, ShoppingListEvent, ShoppingListItem


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
class PurchaseShopGroup:
    shop: Shop
    rows: list[BestOfferResult]
    selected_total: Decimal


@dataclass(frozen=True)
class PurchasePlan:
    rows: list[BestOfferResult]
    groups: list[PurchaseShopGroup]
    total_best_cost: Decimal
    total_source_cost: Decimal
    remaining_best_cost: Decimal
    potential_saving: Decimal


def get_or_create_shopping_list(user):
    shopping_list, _created = ShoppingList.objects.get_or_create(user=user)
    return shopping_list


def add_offer_to_shopping_list(user, offer):
    shopping_list = get_or_create_shopping_list(user)
    item, created = ShoppingListItem.objects.get_or_create(
        shopping_list=shopping_list,
        source_offer=offer,
        defaults={
            "product": offer.product,
            "name": offer.original_name,
        },
    )
    if created:
        record_shopping_list_event(user, offer, ShoppingListEvent.EventType.ADDED, item.name)
    return item


def record_shopping_list_event(user, offer, event_type, item_name):
    return ShoppingListEvent.objects.create(
        user=user,
        shop=offer.shop if offer else None,
        offer=offer,
        event_type=event_type,
        item_name=item_name,
    )


@transaction.atomic
def replace_shopping_list_offer(item, offer):
    user = item.shopping_list.user
    existing_item = (
        ShoppingListItem.objects.select_for_update()
        .filter(shopping_list=item.shopping_list, source_offer=offer)
        .exclude(pk=item.pk)
        .first()
    )
    if existing_item:
        if item.is_purchased and not existing_item.is_purchased:
            existing_item.is_purchased = True
            existing_item.save(update_fields=["is_purchased"])
        item.delete()
        record_shopping_list_event(
            user,
            offer,
            ShoppingListEvent.EventType.REPLACED,
            existing_item.name,
        )
        return existing_item

    item.source_offer = offer
    item.product = offer.product
    item.name = offer.original_name
    item.save(update_fields=["source_offer", "product", "name"])
    record_shopping_list_event(
        user,
        offer,
        ShoppingListEvent.EventType.REPLACED,
        item.name,
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
        .order_by("source_offer__shop__name", "name", "id")
    )
    rows = [get_best_offer(item) for item in items]
    total_best_cost = Decimal("0.00")
    total_source_cost = Decimal("0.00")
    remaining_best_cost = Decimal("0.00")
    grouped_rows = {}

    for row in rows:
        grouped_rows.setdefault(row.item.source_offer.shop, []).append(row)
        if row.best_offer is None or row.best_price is None:
            continue
        total_best_cost += row.best_price
        total_source_cost += row.source_price or row.best_price
        if not row.item.is_purchased:
            remaining_best_cost += row.best_price

    groups = []
    for shop, shop_rows in grouped_rows.items():
        groups.append(
            PurchaseShopGroup(
                shop=shop,
                rows=shop_rows,
                selected_total=sum(
                    (row.source_price or row.best_price or Decimal("0.00") for row in shop_rows),
                    Decimal("0.00"),
                ),
            )
        )

    return PurchasePlan(
        rows=rows,
        groups=groups,
        total_best_cost=total_best_cost,
        total_source_cost=total_source_cost,
        remaining_best_cost=remaining_best_cost,
        potential_saving=sum((row.potential_saving for row in rows), Decimal("0.00")),
    )
