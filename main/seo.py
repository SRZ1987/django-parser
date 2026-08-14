import json
import re
from decimal import Decimal

from django.conf import settings
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _


VALID_BARCODE_RE = re.compile(r"^(?:[0-9]{8}|[0-9]{12}|[0-9]{13}|[0-9]{14})$")
NOINDEX_PREFIXES = (
    "/accounts/",
    "/admin/",
    "/group-purchases/",
    "/my-list/",
    "/out/",
    "/price-comparisons/",
    "/products/",
    "/search/",
    "/shared-list/",
    "/statistics/",
)


def is_valid_barcode(value):
    return bool(VALID_BARCODE_RE.fullmatch((value or "").strip()))


def effective_price(offer):
    if offer.sale_price is not None and (
        offer.price is None or offer.sale_price < offer.price
    ):
        return offer.sale_price
    return offer.price


def absolute_url(request, path):
    if settings.SITE_URL:
        return f"{settings.SITE_URL}{path}"
    return request.build_absolute_uri(path)


def canonical_url(request, path=None):
    return absolute_url(request, path or request.path)


def barcode_schema_property(barcode):
    return {
        8: "gtin8",
        12: "gtin12",
        13: "gtin13",
        14: "gtin14",
    }.get(len(barcode or ""))


def json_ld(data):
    serialized = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )
    serialized = serialized.replace("<", "\\u003c").replace(">", "\\u003e")
    serialized = serialized.replace("&", "\\u0026")
    return mark_safe(serialized)


def product_offer_schema(offer, page_url):
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": offer.original_name,
        "url": page_url,
        "sku": offer.sku or offer.external_id,
    }
    _add_product_details(product, offer)

    price = effective_price(offer)
    if price is not None:
        product["offers"] = {
            "@type": "Offer",
            "price": str(price),
            "priceCurrency": offer.currency or "EUR",
            "availability": "https://schema.org/InStock",
            "url": offer.product_url or page_url,
            "seller": {"@type": "Organization", "name": offer.shop.name},
        }
    return product


def product_comparison_schema(offers, page_url):
    representative = next((offer for offer in offers if offer.image_url), offers[0])
    prices = [effective_price(offer) for offer in offers]
    prices = [price for price in prices if price is not None]
    product = {
        "@type": "Product",
        "name": representative.original_name,
        "url": page_url,
        "sku": representative.sku or representative.external_id,
    }
    _add_product_details(product, representative)
    if prices:
        product["offers"] = {
            "@type": "AggregateOffer",
            "lowPrice": str(min(prices)),
            "highPrice": str(max(prices)),
            "priceCurrency": representative.currency or "EUR",
            "offerCount": len(offers),
        }

    return {
        "@context": "https://schema.org",
        "@graph": [
            product,
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "position": 1,
                        "name": _("Home"),
                        "item": absolute_url_from_page(page_url, reverse("home")),
                    },
                    {
                        "@type": "ListItem",
                        "position": 2,
                        "name": _("Price comparison"),
                        "item": page_url,
                    },
                ],
            },
        ],
    }


def absolute_url_from_page(page_url, path):
    marker = "://"
    if marker not in page_url:
        return path
    scheme, remainder = page_url.split(marker, 1)
    host = remainder.split("/", 1)[0]
    return f"{scheme}{marker}{host}{path}"


def _add_product_details(product, offer):
    barcode_property = barcode_schema_property(offer.barcode)
    if barcode_property and is_valid_barcode(offer.barcode):
        product[barcode_property] = offer.barcode
    if offer.image_url:
        product["image"] = [offer.image_url]
    if offer.description:
        product["description"] = offer.description[:1000]
    if offer.product.brand:
        product["brand"] = {"@type": "Brand", "name": offer.product.brand}
    if offer.product.model:
        product["model"] = offer.product.model
    if offer.category:
        product["category"] = offer.category.name


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
