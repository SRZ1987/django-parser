import hashlib
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.db.models import Count, F, Min, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from catalog.models import ProductOffer, Shop

from .models import DailySiteVisit, ShoppingListEvent, ShoppingListItem, StoreClick


logger = logging.getLogger(__name__)


def visitor_hash_for_request(request):
    visitor_id = request.session.get("_analytics_visitor_id")
    if not visitor_id:
        visitor_id = _anonymous_visitor_id(request)
        request.session["_analytics_visitor_id"] = visitor_id
    value = f"{settings.SECRET_KEY}:{visitor_id}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _anonymous_visitor_id(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    client_ip = forwarded_for.split(",", 1)[0].strip() or request.META.get("REMOTE_ADDR", "")
    user_agent = request.META.get("HTTP_USER_AGENT", "").strip().lower()
    language = request.META.get("HTTP_ACCEPT_LANGUAGE", "").split(",", 1)[0].strip().lower()
    value = f"{settings.SECRET_KEY}:{client_ip}:{user_agent}:{language}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def record_site_visit(request):
    visitor_hash = visitor_hash_for_request(request)
    now = timezone.now()
    visit, created = DailySiteVisit.objects.get_or_create(
        date=timezone.localdate(now),
        visitor_hash=visitor_hash,
        defaults={
            "user": request.user if request.user.is_authenticated else None,
            "first_path": request.path[:500],
            "last_path": request.path[:500],
            "pageviews": 1,
        },
    )
    if not created:
        DailySiteVisit.objects.filter(pk=visit.pk).update(
            pageviews=F("pageviews") + 1,
            last_path=request.path[:500],
            last_seen_at=now,
            **({"user": request.user} if request.user.is_authenticated else {}),
        )


def record_store_click(request, offer):
    return StoreClick.objects.create(
        shop=offer.shop,
        offer=offer,
        user=request.user if request.user.is_authenticated else None,
        visitor_hash=visitor_hash_for_request(request),
        source_path=(request.META.get("HTTP_REFERER") or request.GET.get("from") or "")[:500],
    )


def safely_record_site_visit(request):
    try:
        record_site_visit(request)
    except DatabaseError:
        logger.exception("Could not record site visit")


def build_analytics_dashboard(days=30):
    today = timezone.localdate()
    start_date = today - timedelta(days=days - 1)
    user_model = get_user_model()

    visit_totals = DailySiteVisit.objects.aggregate(
        unique_visitors=Count("visitor_hash", distinct=True),
        visitor_days=Count("id"),
        pageviews=Coalesce(Sum("pageviews"), 0),
        tracking_started=Min("date"),
    )
    today_visits = DailySiteVisit.objects.filter(date=today).aggregate(
        visitors=Count("id"),
        pageviews=Coalesce(Sum("pageviews"), 0),
    )
    click_totals = StoreClick.objects.aggregate(
        total=Count("id"),
        unique_visitors=Count("visitor_hash", distinct=True),
    )

    shops = Shop.objects.annotate(
        active_offers_count=Count(
            "offers",
            filter=Q(offers__is_active=True, offers__is_available=True),
            distinct=True,
        ),
        clicks_count=Count("store_clicks", distinct=True),
        clicks_30d_count=Count(
            "store_clicks",
            filter=Q(store_clicks__clicked_at__date__gte=start_date),
            distinct=True,
        ),
        current_list_items_count=Count("offers__shopping_list_items", distinct=True),
        list_users_count=Count(
            "offers__shopping_list_items__shopping_list__user",
            distinct=True,
        ),
        added_events_count=Count(
            "shopping_list_events",
            filter=Q(shopping_list_events__event_type=ShoppingListEvent.EventType.ADDED),
            distinct=True,
        ),
    ).order_by("-clicks_count", "name")

    top_clicked_offers = (
        ProductOffer.objects.filter(store_clicks__isnull=False)
        .select_related("shop")
        .annotate(clicks_count=Count("store_clicks"))
        .order_by("-clicks_count", "original_name")[:15]
    )
    top_listed_offers = (
        ProductOffer.objects.filter(shopping_list_items__isnull=False)
        .select_related("shop")
        .annotate(
            list_items_count=Count("shopping_list_items"),
            list_users_count=Count("shopping_list_items__shopping_list__user", distinct=True),
        )
        .order_by("-list_items_count", "original_name")[:15]
    )

    visits_by_date = {
        row["date"]: row
        for row in DailySiteVisit.objects.filter(date__gte=start_date)
        .values("date")
        .annotate(visitors=Count("id"), pageviews=Coalesce(Sum("pageviews"), 0))
    }
    registrations_by_date = dict(
        user_model.objects.filter(date_joined__date__gte=start_date)
        .values_list("date_joined__date")
        .annotate(total=Count("id"))
    )
    clicks_by_date = dict(
        StoreClick.objects.filter(clicked_at__date__gte=start_date)
        .values_list("clicked_at__date")
        .annotate(total=Count("id"))
    )
    additions_by_date = dict(
        ShoppingListEvent.objects.filter(
            created_at__date__gte=start_date,
            event_type=ShoppingListEvent.EventType.ADDED,
        )
        .values_list("created_at__date")
        .annotate(total=Count("id"))
    )

    daily_rows = []
    for offset in range(days):
        date = start_date + timedelta(days=offset)
        visit_row = visits_by_date.get(date, {})
        daily_rows.append(
            {
                "date": date,
                "visitors": visit_row.get("visitors", 0),
                "pageviews": visit_row.get("pageviews", 0),
                "registrations": registrations_by_date.get(date, 0),
                "clicks": clicks_by_date.get(date, 0),
                "additions": additions_by_date.get(date, 0),
            }
        )

    return {
        "today": today_visits,
        "visit_totals": visit_totals,
        "click_totals": click_totals,
        "registered_users": user_model.objects.count(),
        "confirmed_users": user_model.objects.filter(is_active=True).exclude(email="").count(),
        "registrations_30d": user_model.objects.filter(date_joined__date__gte=start_date).count(),
        "active_offers": ProductOffer.objects.filter(is_active=True, is_available=True).count(),
        "current_list_items": ShoppingListItem.objects.count(),
        "shops": shops,
        "top_clicked_offers": top_clicked_offers,
        "top_listed_offers": top_listed_offers,
        "daily_rows": reversed(daily_rows),
        "period_days": days,
    }
