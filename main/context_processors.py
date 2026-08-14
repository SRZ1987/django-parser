from django.db.models import Count
from django.conf import settings
from django.utils.translation import gettext as _

from .models import ShoppingList
from .seo import NOINDEX_PREFIXES, canonical_url


def shopping_list_summary(request):
    if not request.user.is_authenticated:
        return {"shopping_list_item_count": None}

    item_count = (
        ShoppingList.objects.filter(user_id=request.user.pk)
        .order_by()
        .annotate(item_count=Count("items"))
        .values_list("item_count", flat=True)
        .first()
    )
    return {"shopping_list_item_count": item_count}


def seo_defaults(request):
    noindex = request.path.startswith(NOINDEX_PREFIXES)
    return {
        "seo_title": _("Compare prices in Estonian stores — Tannenberg"),
        "seo_description": _(
            "Compare current product prices across Estonian stores and find the best offer."
        ),
        "seo_canonical_url": canonical_url(request),
        "seo_robots": "noindex,nofollow" if noindex else "index,follow",
        "google_site_verification": settings.GOOGLE_SITE_VERIFICATION,
    }
