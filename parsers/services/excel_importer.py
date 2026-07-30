import hashlib
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone
from openpyxl import load_workbook

from catalog.models import PriceHistory, Product, ProductOffer
from catalog.services.normalization import normalize_product_name

from .excel_validation import parse_decimal


@dataclass
class ExcelImportResult:
    products_found: int = 0
    products_created: int = 0
    products_updated: int = 0
    prices_changed: int = 0
    skipped_rows: int = 0
    errors_count: int = 0


class ExcelCatalogImporter:
    DEACTIVATE_BATCH_SIZE = 1000
    MIN_ACTIVE_OFFERS_FOR_ANOMALY_CHECK = 100
    MIN_REMOTE_TO_ACTIVE_RATIO = 0.2

    def import_file(self, parser_export, *, column_map, worksheet_name=None, deactivate_missing=True):
        result = ExcelImportResult()
        shop = parser_export.shop
        seen_at = timezone.now()
        remote_external_ids = set()

        workbook = load_workbook(parser_export.file.path, read_only=True, data_only=True)
        try:
            worksheet = workbook[worksheet_name] if worksheet_name else workbook.active
            headers = [cell.value for cell in next(worksheet.iter_rows(min_row=1, max_row=1))]
            header_index = {header: index for index, header in enumerate(headers)}

            for row in worksheet.iter_rows(min_row=2, values_only=True):
                if not any(value not in (None, "") for value in row):
                    continue
                try:
                    parsed = self._parse_row(row, header_index, column_map)
                    if not parsed["external_id"] or not parsed["original_name"]:
                        result.skipped_rows += 1
                        continue
                    remote_external_ids.add(parsed["external_id"])
                    created, price_changed = self._save_offer(shop, parsed, seen_at)
                    result.products_found += 1
                    result.products_created += int(created)
                    result.products_updated += int(not created)
                    result.prices_changed += int(price_changed)
                except Exception:
                    result.errors_count += 1
                    result.skipped_rows += 1
        finally:
            workbook.close()

        if result.products_found <= 0:
            raise ValueError("Excel import produced no valid product rows.")

        if deactivate_missing:
            self._validate_catalog_size(shop, remote_external_ids)
            self._deactivate_missing_offers(shop, remote_external_ids)

        parser_export.imported_at = timezone.now()
        parser_export.import_success = True
        parser_export.save(update_fields=["imported_at", "import_success"])
        return result

    def _parse_row(self, row, header_index, column_map):
        data = {}
        for column, target in column_map.items():
            index = header_index[column]
            data[target] = clean_value(row[index])

        external_id = data.get("external_id") or stable_external_id(data.get("product_url"), data.get("original_name"))
        price = parse_decimal(data.get("price"))
        sale_price = parse_decimal(data.get("sale_price"))
        if price is None and sale_price is not None:
            price = sale_price
            sale_price = None
        if price is not None and sale_price is not None and sale_price >= price:
            sale_price = None

        return {
            "external_id": external_id,
            "sku": data.get("external_id", ""),
            "barcode": data.get("barcode", ""),
            "original_name": data.get("original_name", ""),
            "price": price,
            "sale_price": sale_price,
            "product_url": data.get("product_url", ""),
            "image_url": data.get("image_url", ""),
        }

    @transaction.atomic
    def _save_offer(self, shop, parsed, seen_at):
        offer = ProductOffer.objects.select_related("product").filter(shop=shop, external_id=parsed["external_id"]).first()
        created = offer is None
        if created:
            product = Product.objects.create(
                name=parsed["original_name"],
                barcode=parsed["barcode"],
            )
            offer = ProductOffer(shop=shop, product=product, external_id=parsed["external_id"])
        else:
            product = offer.product
            product.name = parsed["original_name"]
            if parsed["barcode"]:
                product.barcode = parsed["barcode"]
            product.normalized_name = normalize_product_name(parsed["original_name"])
            product.save(update_fields=["name", "normalized_name", "barcode", "updated_at"])

        previous_price = offer.price
        previous_sale_price = offer.sale_price
        offer.original_name = parsed["original_name"]
        offer.sku = parsed["sku"]
        if parsed["barcode"]:
            offer.barcode = parsed["barcode"]
        offer.price = parsed["price"] if parsed["price"] is not None else offer.price
        offer.sale_price = parsed["sale_price"]
        offer.currency = "EUR"
        if parsed["product_url"]:
            offer.product_url = parsed["product_url"]
        if parsed["image_url"]:
            offer.image_url = parsed["image_url"]
        offer.is_active = True
        offer.is_available = True
        offer.last_seen_at = seen_at
        offer.save()

        price_changed = previous_price != offer.price or previous_sale_price != offer.sale_price
        if (created and (offer.price is not None or offer.sale_price is not None)) or price_changed:
            PriceHistory.objects.create(offer=offer, price=offer.price, sale_price=offer.sale_price)
            return created, True
        return created, False

    def _validate_catalog_size(self, shop, remote_external_ids):
        active_count = ProductOffer.objects.filter(shop=shop, is_active=True).count()
        if active_count < self.MIN_ACTIVE_OFFERS_FOR_ANOMALY_CHECK:
            return
        if len(remote_external_ids) >= active_count * self.MIN_REMOTE_TO_ACTIVE_RATIO:
            return
        raise ValueError(
            f"Excel import returned an anomalously small product list: {len(remote_external_ids)} "
            f"remote products for {active_count} active offers."
        )

    def _deactivate_missing_offers(self, shop, remote_external_ids):
        missing_offer_ids = [
            offer_id
            for offer_id, external_id in ProductOffer.objects.filter(shop=shop).values_list("id", "external_id")
            if external_id not in remote_external_ids
        ]
        for start in range(0, len(missing_offer_ids), self.DEACTIVATE_BATCH_SIZE):
            ProductOffer.objects.filter(id__in=missing_offer_ids[start : start + self.DEACTIVATE_BATCH_SIZE]).update(
                is_active=False,
                is_available=False,
            )


def clean_value(value):
    return "" if value in (None, "") else str(value).strip()


def stable_external_id(*parts):
    source = "|".join(clean_value(part) for part in parts if clean_value(part))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:32] if source else ""
