from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import ShoppingList, ShoppingListItem
from .services import get_best_offer


@dataclass(frozen=True)
class PriceSnapshot:
    source_price: Decimal | None
    best_price: Decimal | None
    best_offer_id: int | None
    best_offer: object | None


@dataclass(frozen=True)
class PriceAlertChange:
    item: ShoppingListItem
    previous_source_price: Decimal | None
    current_source_price: Decimal | None
    source_price_direction: str | None
    best_offer: object | None
    best_price: Decimal | None
    cheaper_offer_improved: bool
    potential_saving: Decimal


@dataclass
class PriceAlertDeliveryResult:
    lists_checked: int = 0
    emails_sent: int = 0
    changes_found: int = 0
    errors_count: int = 0


SNAPSHOT_UPDATE_FIELDS = [
    "price_alert_source_price",
    "price_alert_best_price",
    "price_alert_best_offer",
    "price_alert_checked_at",
]


def set_shopping_list_price_alerts(shopping_list, enabled):
    now = timezone.now()
    shopping_list.price_alerts_enabled = enabled
    shopping_list.price_alerts_enabled_at = now if enabled else None
    shopping_list.save(update_fields=["price_alerts_enabled", "price_alerts_enabled_at", "updated_at"])

    if enabled:
        refresh_shopping_list_price_alert_baseline(shopping_list, checked_at=now)
    else:
        shopping_list.items.update(
            price_alert_source_price=None,
            price_alert_best_price=None,
            price_alert_best_offer=None,
            price_alert_checked_at=None,
        )


def refresh_shopping_list_price_alert_baseline(shopping_list, checked_at=None):
    checked_at = checked_at or timezone.now()
    items = list(_alert_items(shopping_list))
    for item in items:
        _apply_snapshot(item, _get_snapshot(item), checked_at)
    if items:
        ShoppingListItem.objects.bulk_update(items, SNAPSHOT_UPDATE_FIELDS)


def refresh_item_price_alert_baseline(item, checked_at=None):
    _apply_snapshot(item, _get_snapshot(item), checked_at or timezone.now())
    item.save(update_fields=SNAPSHOT_UPDATE_FIELDS)


def reset_item_price_alert_baseline(item):
    item.price_alert_source_price = None
    item.price_alert_best_price = None
    item.price_alert_best_offer = None
    item.price_alert_checked_at = None


def send_shopping_list_price_alerts(log_callback=None):
    log = log_callback or (lambda _message: None)
    result = PriceAlertDeliveryResult()
    shopping_lists = ShoppingList.objects.filter(
        price_alerts_enabled=True,
        user__is_active=True,
    ).select_related("user")

    for shopping_list in shopping_lists.iterator():
        result.lists_checked += 1
        user = shopping_list.user
        if not user.email:
            result.errors_count += 1
            log(f"Price alerts skipped for shopping list {shopping_list.pk}: user has no email.")
            continue

        checked_at = timezone.now()
        evaluated_items = []
        changes = []
        for item in _alert_items(shopping_list):
            try:
                snapshot = _get_snapshot(item)
            except Exception as exc:
                result.errors_count += 1
                log(
                    f"Price alert evaluation failed for shopping list item {item.pk}: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue

            change = _build_change(item, snapshot)
            if change is not None:
                changes.append(change)
            _apply_snapshot(item, snapshot, checked_at)
            evaluated_items.append(item)

        if changes:
            try:
                send_mail(
                    _("Prices in your Tannenberg list have changed"),
                    render_to_string(
                        "main/emails/shopping_list_price_alert.txt",
                        {
                            "user": user,
                            "changes": changes,
                            "shopping_list_url": _shopping_list_url(),
                        },
                    ),
                    settings.DEFAULT_FROM_EMAIL,
                    [user.email],
                    fail_silently=False,
                )
            except Exception as exc:
                result.errors_count += 1
                log(
                    f"Price alert email failed for shopping list {shopping_list.pk}: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue

            shopping_list.price_alerts_last_sent_at = checked_at
            shopping_list.save(update_fields=["price_alerts_last_sent_at", "updated_at"])
            result.emails_sent += 1
            result.changes_found += len(changes)

        if evaluated_items:
            ShoppingListItem.objects.bulk_update(evaluated_items, SNAPSHOT_UPDATE_FIELDS)

    log(
        "Shopping list price alerts: "
        f"lists={result.lists_checked}, emails={result.emails_sent}, "
        f"changes={result.changes_found}, errors={result.errors_count}"
    )
    return result


def _alert_items(shopping_list):
    return shopping_list.items.select_related(
        "product",
        "source_offer",
        "source_offer__shop",
        "source_offer__category",
        "source_offer__product",
        "price_alert_best_offer",
    ).order_by("id")


def _get_snapshot(item):
    best = get_best_offer(item)
    return PriceSnapshot(
        source_price=best.source_price,
        best_price=best.best_price,
        best_offer_id=best.best_offer.pk if best.best_offer else None,
        best_offer=best.best_offer,
    )


def _build_change(item, snapshot):
    if item.price_alert_checked_at is None:
        return None

    direction = None
    previous_source_price = item.price_alert_source_price
    if previous_source_price is not None and snapshot.source_price is not None:
        if snapshot.source_price > previous_source_price:
            direction = "up"
        elif snapshot.source_price < previous_source_price:
            direction = "down"

    previous_cheaper = _is_cheaper_offer(
        item.source_offer_id,
        item.price_alert_best_offer_id,
        item.price_alert_best_price,
        previous_source_price,
    )
    current_cheaper = _is_cheaper_offer(
        item.source_offer_id,
        snapshot.best_offer_id,
        snapshot.best_price,
        snapshot.source_price,
    )
    cheaper_offer_improved = current_cheaper and (
        not previous_cheaper
        or item.price_alert_best_price is None
        or snapshot.best_price < item.price_alert_best_price
        or (
            snapshot.best_price == item.price_alert_best_price
            and snapshot.best_offer_id != item.price_alert_best_offer_id
        )
    )

    if direction is None and not cheaper_offer_improved:
        return None

    potential_saving = Decimal("0.00")
    if current_cheaper:
        potential_saving = snapshot.source_price - snapshot.best_price

    return PriceAlertChange(
        item=item,
        previous_source_price=previous_source_price,
        current_source_price=snapshot.source_price,
        source_price_direction=direction,
        best_offer=snapshot.best_offer,
        best_price=snapshot.best_price,
        cheaper_offer_improved=cheaper_offer_improved,
        potential_saving=potential_saving,
    )


def _is_cheaper_offer(source_offer_id, best_offer_id, best_price, source_price):
    return (
        best_offer_id is not None
        and best_offer_id != source_offer_id
        and best_price is not None
        and source_price is not None
        and best_price < source_price
    )


def _apply_snapshot(item, snapshot, checked_at):
    item.price_alert_source_price = snapshot.source_price
    item.price_alert_best_price = snapshot.best_price
    item.price_alert_best_offer_id = snapshot.best_offer_id
    item.price_alert_checked_at = checked_at


def _shopping_list_url():
    base_url = getattr(settings, "SITE_URL", "").rstrip("/")
    if not base_url:
        trusted_origins = getattr(settings, "CSRF_TRUSTED_ORIGINS", [])
        base_url = next((origin.rstrip("/") for origin in trusted_origins if "*" not in origin), "")
    return f"{base_url}{reverse('shopping_list')}" if base_url else reverse("shopping_list")
