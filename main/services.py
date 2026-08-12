import logging
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from catalog.models import ProductOffer, Shop
from catalog.services.product_matching import offers_are_comparable
from catalog.services.product_search import find_matches

from .models import GroupPurchase, ShoppingList, ShoppingListEvent, ShoppingListItem


logger = logging.getLogger(__name__)


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
    source_total: Decimal | None = None
    best_total: Decimal | None = None
    group_purchase: GroupPurchase | None = None
    group_participant_count: int = 0
    group_quantity_count: int = 0


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


def add_offer_to_shopping_list(user, offer, *, quantity=1):
    quantity = int(quantity)
    if not 1 <= quantity <= 9999:
        raise ValueError("Shopping list quantity must be between 1 and 9999.")
    shopping_list = get_or_create_shopping_list(user)
    item, created = ShoppingListItem.objects.get_or_create(
        shopping_list=shopping_list,
        source_offer=offer,
        defaults={
            "product": offer.product,
            "name": offer.original_name,
            "quantity": quantity,
        },
    )
    if created:
        record_shopping_list_event(user, offer, ShoppingListEvent.EventType.ADDED, item.name)
        if shopping_list.price_alerts_enabled:
            try:
                from .price_alerts import refresh_item_price_alert_baseline

                refresh_item_price_alert_baseline(item)
            except Exception:
                logger.exception("Could not initialize price alert baseline for shopping list item %s", item.pk)
    _ensure_group_membership_safely(item)
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
    _detach_group_membership_safely(item)
    existing_item = (
        ShoppingListItem.objects.select_for_update()
        .filter(shopping_list=item.shopping_list, source_offer=offer)
        .exclude(pk=item.pk)
        .first()
    )
    if existing_item:
        existing_item.quantity = min(9999, existing_item.quantity + item.quantity)
        fields_to_update = ["quantity"]
        if item.is_purchased and not existing_item.is_purchased:
            existing_item.is_purchased = True
            fields_to_update.append("is_purchased")
        existing_item.save(update_fields=fields_to_update)
        item.delete()
        _ensure_group_membership_safely(existing_item)
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
    item.price_alert_source_price = None
    item.price_alert_best_price = None
    item.price_alert_best_offer = None
    item.price_alert_checked_at = None
    item.save(
        update_fields=[
            "source_offer",
            "product",
            "name",
            "price_alert_source_price",
            "price_alert_best_price",
            "price_alert_best_offer",
            "price_alert_checked_at",
        ]
    )
    _ensure_group_membership_safely(item)
    if item.shopping_list.price_alerts_enabled:
        try:
            from .price_alerts import refresh_item_price_alert_baseline

            refresh_item_price_alert_baseline(item)
        except Exception:
            logger.exception("Could not reset price alert baseline for shopping list item %s", item.pk)
    record_shopping_list_event(
        user,
        offer,
        ShoppingListEvent.EventType.REPLACED,
        item.name,
    )
    return item


def get_best_offer(item):
    group_purchase, participant_count, quantity_count = _item_group_purchase(item)
    matches = find_matches(item.source_offer)
    candidate_matches = matches.exact_matches + matches.same_product + matches.similar_products
    offers = [
        match.offer
        for match in candidate_matches
        if match.offer.price_for_quantity(item.quantity) is not None
        and match.offer.shop_id != item.source_offer.shop_id
        and offers_are_comparable(item.source_offer, match.offer)
    ]

    if item.source_offer.current_price is not None and item.source_offer not in offers:
        offers.append(item.source_offer)

    if not offers:
        return BestOfferResult(
            item=item,
            best_offer=None,
            group_purchase=group_purchase,
            group_participant_count=participant_count,
            group_quantity_count=quantity_count,
        )

    offers = sorted(
        offers,
        key=lambda offer: (
            offer.price_for_quantity(item.quantity),
            offer.shop.name,
            offer.original_name,
        ),
    )
    best_offer = offers[0]
    offer_prices = [offer.price_for_quantity(item.quantity) for offer in offers]
    highest_price = max(offer_prices)
    best_price = best_offer.price_for_quantity(item.quantity)
    source_price = item.source_offer.price_for_quantity(item.quantity)
    source_total = (source_price or best_price) * item.quantity
    best_total = best_price * item.quantity
    potential_saving = max(Decimal("0.00"), source_total - best_total)
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
        source_total=source_total,
        best_total=best_total,
        group_purchase=group_purchase,
        group_participant_count=participant_count,
        group_quantity_count=quantity_count,
    )


def build_purchase_plan(shopping_list):
    items = (
        shopping_list.items.select_related(
            "product",
            "source_offer",
            "source_offer__shop",
            "source_offer__category",
            "source_offer__product",
            "group_purchase_membership__group",
        )
        .prefetch_related("group_purchase_membership__group__members")
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
        total_best_cost += row.best_total
        total_source_cost += row.source_total
        if not row.item.is_purchased:
            remaining_best_cost += row.best_total

    groups = []
    for shop, shop_rows in grouped_rows.items():
        groups.append(
            PurchaseShopGroup(
                shop=shop,
                rows=shop_rows,
                selected_total=sum(
                    (row.source_total or Decimal("0.00") for row in shop_rows),
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


def _item_group_purchase(item):
    try:
        membership = item.group_purchase_membership
    except ObjectDoesNotExist:
        return None, 0, 0
    group = membership.group
    if group.status != GroupPurchase.Status.OPEN:
        return None, 0, 0
    members = list(group.members.all())
    return group, len(members), sum(member.quantity for member in members)


def _ensure_group_membership_safely(item):
    try:
        from .group_purchases import ensure_group_purchase_membership

        return ensure_group_purchase_membership(item)
    except Exception:
        logger.exception("Could not synchronize group purchase membership for item %s", item.pk)
        return None


def _detach_group_membership_safely(item):
    try:
        from .group_purchases import detach_group_purchase_membership

        return detach_group_purchase_membership(item)
    except Exception:
        logger.exception("Could not remove group purchase membership for item %s", item.pk)
        return None
