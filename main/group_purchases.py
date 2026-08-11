from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone

from .models import GroupPurchase, GroupPurchaseMember


def offer_supports_group_purchase(offer):
    current_price = offer.current_price
    return bool(
        offer.is_active
        and offer.is_available
        and current_price is not None
        and offer.quantity_price is not None
        and offer.quantity_price_min_quantity is not None
        and offer.quantity_price_min_quantity >= 2
        and offer.quantity_price < current_price
    )


def group_purchase_cutoff(now=None):
    now = now or timezone.now()
    inactivity_days = max(1, settings.GROUP_PURCHASE_INACTIVITY_DAYS)
    return now - timedelta(days=inactivity_days)


def cleanup_group_purchases(*, now=None):
    now = now or timezone.now()
    open_groups = GroupPurchase.objects.filter(status=GroupPurchase.Status.OPEN)
    stale_count = open_groups.filter(
        last_activity_at__lt=group_purchase_cutoff(now),
    ).update(
        status=GroupPurchase.Status.EXPIRED,
        closed_at=now,
    )

    invalid_group_ids = [
        group.pk
        for group in open_groups.select_related("offer")
        if not offer_supports_group_purchase(group.offer)
    ]
    invalid_count = GroupPurchase.objects.filter(
        pk__in=invalid_group_ids,
        status=GroupPurchase.Status.OPEN,
    ).update(
        status=GroupPurchase.Status.CLOSED,
        closed_at=now,
    )

    empty_group_ids = list(
        GroupPurchase.objects.filter(status=GroupPurchase.Status.OPEN)
        .annotate(member_count=Count("members"))
        .filter(member_count=0)
        .values_list("pk", flat=True)
    )
    empty_count = GroupPurchase.objects.filter(
        pk__in=empty_group_ids,
        status=GroupPurchase.Status.OPEN,
    ).update(
        status=GroupPurchase.Status.CLOSED,
        closed_at=now,
    )
    return {
        "stale": stale_count,
        "invalid": invalid_count,
        "empty": empty_count,
    }


@transaction.atomic
def ensure_group_purchase_membership(item):
    offer = item.source_offer
    now = timezone.now()
    GroupPurchase.objects.filter(
        offer=offer,
        status=GroupPurchase.Status.OPEN,
        last_activity_at__lt=group_purchase_cutoff(now),
    ).update(
        status=GroupPurchase.Status.EXPIRED,
        closed_at=now,
    )

    existing_membership = (
        GroupPurchaseMember.objects.select_related("group")
        .filter(shopping_list_item=item)
        .first()
    )
    if not offer_supports_group_purchase(offer):
        if existing_membership:
            old_group_id = existing_membership.group_id
            existing_membership.delete()
            close_group_if_empty(old_group_id, now=now)
        return None

    if existing_membership and existing_membership.group.status != GroupPurchase.Status.OPEN:
        existing_membership.delete()
        existing_membership = None

    group = (
        GroupPurchase.objects.select_for_update()
        .filter(offer=offer, status=GroupPurchase.Status.OPEN)
        .first()
    )
    if group is None:
        try:
            with transaction.atomic():
                group = GroupPurchase.objects.create(
                    offer=offer,
                    target_quantity=offer.quantity_price_min_quantity,
                    quantity_price=offer.quantity_price,
                    last_activity_at=now,
                )
        except IntegrityError:
            group = GroupPurchase.objects.select_for_update().get(
                offer=offer,
                status=GroupPurchase.Status.OPEN,
            )

    fields_to_update = []
    if group.target_quantity != offer.quantity_price_min_quantity:
        group.target_quantity = offer.quantity_price_min_quantity
        fields_to_update.append("target_quantity")
    if group.quantity_price != offer.quantity_price:
        group.quantity_price = offer.quantity_price
        fields_to_update.append("quantity_price")

    membership, created = GroupPurchaseMember.objects.get_or_create(
        group=group,
        user=item.shopping_list.user,
        defaults={"shopping_list_item": item},
    )
    if membership.shopping_list_item_id != item.pk:
        membership.shopping_list_item = item
        membership.save(update_fields=["shopping_list_item"])
    if created:
        group.last_activity_at = now
        fields_to_update.append("last_activity_at")
    if fields_to_update:
        group.save(update_fields=[*fields_to_update, "updated_at"])
    return membership


def sync_shopping_list_group_memberships(shopping_list):
    items = (
        shopping_list.items.filter(group_purchase_membership__isnull=True)
        .select_related("shopping_list", "source_offer")
        .order_by("pk")
    )
    for item in items:
        ensure_group_purchase_membership(item)


@transaction.atomic
def detach_group_purchase_membership(item):
    membership = (
        GroupPurchaseMember.objects.select_related("group")
        .filter(shopping_list_item=item)
        .first()
    )
    if membership is None:
        return None
    group_id = membership.group_id
    membership.delete()
    close_group_if_empty(group_id)
    return group_id


@transaction.atomic
def close_group_if_empty(group_id, *, now=None):
    group = (
        GroupPurchase.objects.select_for_update()
        .filter(pk=group_id, status=GroupPurchase.Status.OPEN)
        .first()
    )
    if group is None or group.members.exists():
        return False
    group.status = GroupPurchase.Status.CLOSED
    group.closed_at = now or timezone.now()
    group.save(update_fields=["status", "closed_at", "updated_at"])
    return True


def active_group_purchases():
    return GroupPurchase.objects.filter(
        status=GroupPurchase.Status.OPEN,
        offer__is_active=True,
        offer__is_available=True,
        members__isnull=False,
    ).distinct()


def touch_group_purchase(group, *, now=None):
    now = now or timezone.now()
    return GroupPurchase.objects.filter(
        pk=group.pk,
        status=GroupPurchase.Status.OPEN,
    ).update(last_activity_at=now)
