from django.contrib.sitemaps import Sitemap
from django.db.models import Count, Q
from django.urls import reverse

from catalog.models import ProductOffer


class StaticSitemap(Sitemap):
    changefreq = "daily"
    priority = 0.7

    def items(self):
        return ("home",)

    def location(self, item):
        return reverse(item)


class BarcodeComparisonSitemap(Sitemap):
    changefreq = "daily"
    priority = 0.9
    limit = 50000

    def items(self):
        return (
            ProductOffer.objects.filter(
                is_active=True,
                is_available=True,
                shop__is_active=True,
                barcode__regex=r"^([0-9]{8}|[0-9]{12}|[0-9]{13}|[0-9]{14})$",
            )
            .filter(Q(price__isnull=False) | Q(sale_price__isnull=False))
            .order_by()
            .values("barcode")
            .annotate(shop_count=Count("shop_id", distinct=True))
            .filter(shop_count__gte=2)
            .order_by("barcode")
            .values_list("barcode", flat=True)
        )

    def location(self, barcode):
        return reverse("barcode_product_detail", args=[barcode])


sitemaps = {
    "static": StaticSitemap,
    "products": BarcodeComparisonSitemap,
}
