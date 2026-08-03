from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from catalog.models import Product, ProductOffer, Shop
from parsers.services.bauhaus_barcode_enricher import (
    BarcodeFetchResult,
    BauhausBarcodeEnrichmentResult,
    enrich_bauhaus_offer_barcodes,
)


VALID_EAN = "4006381333931"


class BauhausBarcodeEnricherTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="BAUHAUS", code="bauhaus")

    def create_offer(self, external_id, *, barcode="", checked_at=None, product_url=None):
        product = Product.objects.create(name=f"Product {external_id}", barcode=barcode)
        return ProductOffer.objects.create(
            shop=self.shop,
            product=product,
            external_id=external_id,
            sku=external_id,
            barcode=barcode,
            barcode_checked_at=checked_at,
            original_name=product.name,
            product_url=product_url or f"https://www.bauhaus.ee/{external_id}",
        )

    def test_found_ean_updates_offer_and_product(self):
        offer = self.create_offer("SKU-1")

        async def fake_fetch(targets, concurrency):
            return [BarcodeFetchResult(offer_id=offer.pk, ean=VALID_EAN, source="jsonld_gtin")]

        with patch("parsers.services.bauhaus_barcode_enricher._fetch_barcodes", fake_fetch):
            result = enrich_bauhaus_offer_barcodes([offer.pk])

        offer.refresh_from_db()
        offer.product.refresh_from_db()
        self.assertEqual(result.checked, 1)
        self.assertEqual(result.found, 1)
        self.assertEqual(offer.barcode, VALID_EAN)
        self.assertEqual(offer.product.barcode, VALID_EAN)
        self.assertIsNotNone(offer.barcode_checked_at)

    def test_missing_ean_marks_offer_as_checked(self):
        offer = self.create_offer("SKU-1")

        async def fake_fetch(targets, concurrency):
            return [BarcodeFetchResult(offer_id=offer.pk, ean="", source="ean_not_found")]

        with patch("parsers.services.bauhaus_barcode_enricher._fetch_barcodes", fake_fetch):
            result = enrich_bauhaus_offer_barcodes([offer.pk])

        offer.refresh_from_db()
        self.assertEqual(result.not_found, 1)
        self.assertIsNotNone(offer.barcode_checked_at)

    def test_one_card_error_does_not_stop_other_offers(self):
        failed_offer = self.create_offer("SKU-FAIL")
        successful_offer = self.create_offer("SKU-OK")

        async def fake_fetch_product_ean(session, semaphore, controller, sku, product_url):
            if sku == failed_offer.sku:
                raise TimeoutError("product page timed out")
            return sku, VALID_EAN, "next_data_gtin", product_url

        logs = []
        with patch("parsers.services.bauhaus_barcode_enricher.fetch_product_ean", fake_fetch_product_ean):
            result = enrich_bauhaus_offer_barcodes(
                [failed_offer.pk, successful_offer.pk],
                log_callback=logs.append,
            )

        failed_offer.refresh_from_db()
        successful_offer.refresh_from_db()
        self.assertEqual(result.checked, 2)
        self.assertEqual(result.errors, 1)
        self.assertEqual(result.found, 1)
        self.assertIsNotNone(failed_offer.barcode_checked_at)
        self.assertEqual(successful_offer.barcode, VALID_EAN)
        self.assertTrue(any("WARNING" in message for message in logs))

    def test_existing_barcode_and_checked_offer_are_skipped(self):
        barcode_offer = self.create_offer("SKU-BARCODE", barcode=VALID_EAN)
        checked_offer = self.create_offer("SKU-CHECKED", checked_at=timezone.now())

        with patch("parsers.services.bauhaus_barcode_enricher._fetch_barcodes") as fetch:
            result = enrich_bauhaus_offer_barcodes([barcode_offer.pk, checked_offer.pk])

        self.assertEqual(result.checked, 0)
        fetch.assert_not_called()


class EnrichBauhausBarcodesCommandTests(TestCase):
    def setUp(self):
        self.bauhaus = Shop.objects.create(name="BAUHAUS", code="bauhaus")
        self.espak = Shop.objects.create(name="ESPAK", code="espak")

    def create_offer(self, shop, external_id, *, barcode="", checked_at=None):
        product = Product.objects.create(name=external_id, barcode=barcode)
        return ProductOffer.objects.create(
            shop=shop,
            product=product,
            external_id=external_id,
            sku=external_id,
            barcode=barcode,
            barcode_checked_at=checked_at,
            original_name=external_id,
            product_url=f"https://example.test/{external_id}",
        )

    def test_default_command_selects_only_unchecked_bauhaus_offers(self):
        pending = self.create_offer(self.bauhaus, "PENDING")
        self.create_offer(self.bauhaus, "CHECKED", checked_at=timezone.now())
        self.create_offer(self.bauhaus, "FILLED", barcode=VALID_EAN)
        self.create_offer(self.espak, "OTHER-SHOP")
        service_result = BauhausBarcodeEnrichmentResult(checked=1, found=1)

        with patch(
            "parsers.management.commands.enrich_bauhaus_barcodes.enrich_bauhaus_offer_barcodes",
            return_value=service_result,
        ) as enrich:
            call_command("enrich_bauhaus_barcodes", stdout=StringIO())

        self.assertEqual(enrich.call_args.args[0], [pending.pk])
        self.assertFalse(enrich.call_args.kwargs["retry_missing"])

    def test_retry_missing_selects_previously_checked_offers_without_ean(self):
        pending = self.create_offer(self.bauhaus, "PENDING")
        checked = self.create_offer(self.bauhaus, "CHECKED", checked_at=timezone.now())
        self.create_offer(self.bauhaus, "FILLED", barcode=VALID_EAN, checked_at=timezone.now())
        service_result = BauhausBarcodeEnrichmentResult(checked=2, not_found=2)

        with patch(
            "parsers.management.commands.enrich_bauhaus_barcodes.enrich_bauhaus_offer_barcodes",
            return_value=service_result,
        ) as enrich:
            call_command("enrich_bauhaus_barcodes", "--retry-missing", stdout=StringIO())

        self.assertEqual(enrich.call_args.args[0], [pending.pk, checked.pk])
        self.assertTrue(enrich.call_args.kwargs["retry_missing"])
