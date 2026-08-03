import asyncio
import threading
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from catalog.models import Product, ProductOffer, Shop
from parsers.services import bauhaus_barcode_enricher
from parsers.models import ParserConfig
from parsers.services.bauhaus_barcode_enricher import (
    BarcodeTarget,
    BarcodeFetchMetrics,
    BarcodeFetchResult,
    BauhausBarcodeEnrichmentResult,
    FastAdaptiveController,
    ProductPageResult,
    _fetch_barcodes,
    _fetch_product_page,
    enrich_bauhaus_offer_barcodes,
    extract_related_product_eans,
)
from parsers.standalone.bauhaus_parser import AdjustableLimiter


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

        async def fake_fetch(
            targets,
            concurrency,
            minimum=1,
            result_callback=None,
            stop_event=None,
        ):
            item = BarcodeFetchResult(
                offer_id=offer.pk,
                ean=VALID_EAN,
                source="jsonld_gtin",
            )
            if result_callback is not None:
                result_callback(item)
                return []
            return [item]

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

        async def fake_fetch(
            targets,
            concurrency,
            minimum=1,
            result_callback=None,
            stop_event=None,
        ):
            item = BarcodeFetchResult(
                offer_id=offer.pk,
                ean="",
                source="ean_not_found",
            )
            if result_callback is not None:
                result_callback(item)
                return []
            return [item]

        with patch("parsers.services.bauhaus_barcode_enricher._fetch_barcodes", fake_fetch):
            result = enrich_bauhaus_offer_barcodes([offer.pk])

        offer.refresh_from_db()
        self.assertEqual(result.not_found, 1)
        self.assertIsNotNone(offer.barcode_checked_at)

    def test_one_card_error_does_not_stop_other_offers(self):
        failed_offer = self.create_offer("SKU-FAIL")
        successful_offer = self.create_offer("SKU-OK")

        async def fake_fetch_product_page(
            session,
            limiter,
            controller,
            sku,
            product_url,
            **kwargs,
        ):
            if sku == failed_offer.sku:
                raise TimeoutError("product page timed out")
            return ProductPageResult(
                document=(
                    '<script type="application/ld+json">'
                    f'{{"@type":"Product","sku":"{sku}",'
                    f'"gtin13":"{VALID_EAN}"}}'
                    "</script>"
                ),
                source="html",
                resolved_url=product_url,
                requests=1,
            )

        logs = []
        with patch(
            "parsers.services.bauhaus_barcode_client.fetch_product_page",
            fake_fetch_product_page,
        ):
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

    def test_ean_is_persisted_before_remaining_download_finishes(self):
        first_offer = self.create_offer("SKU-FIRST")
        second_offer = self.create_offer("SKU-SECOND")
        first_persisted = threading.Event()
        original_persist = bauhaus_barcode_enricher._persist_fetch_result

        def tracking_persist(item, *, product_id):
            outcome = original_persist(item, product_id=product_id)
            if item.offer_id == first_offer.pk:
                first_persisted.set()
            return outcome

        async def fake_fetch(
            targets,
            concurrency,
            minimum=1,
            result_callback=None,
            stop_event=None,
        ):
            result_callback(
                BarcodeFetchResult(
                    offer_id=first_offer.pk,
                    ean=VALID_EAN,
                    source="jsonld_gtin",
                )
            )
            deadline = asyncio.get_running_loop().time() + 2
            while not first_persisted.is_set():
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("First EAN was not persisted during download")
                await asyncio.sleep(0.01)
            result_callback(
                BarcodeFetchResult(
                    offer_id=second_offer.pk,
                    ean=VALID_EAN,
                    source="jsonld_gtin",
                )
            )
            return []

        with (
            patch(
                "parsers.services.bauhaus_barcode_enricher._fetch_barcodes",
                fake_fetch,
            ),
            patch(
                "parsers.services.bauhaus_barcode_enricher._persist_fetch_result",
                side_effect=tracking_persist,
            ),
        ):
            result = enrich_bauhaus_offer_barcodes(
                [first_offer.pk, second_offer.pk],
            )

        first_offer.refresh_from_db()
        second_offer.refresh_from_db()
        self.assertEqual(result.found, 2)
        self.assertEqual(first_offer.barcode, VALID_EAN)
        self.assertEqual(second_offer.barcode, VALID_EAN)

    def test_completed_ean_survives_later_download_interruption(self):
        completed_offer = self.create_offer("SKU-COMPLETED")
        pending_offer = self.create_offer("SKU-PENDING")
        first_persisted = threading.Event()
        original_persist = bauhaus_barcode_enricher._persist_fetch_result

        def tracking_persist(item, *, product_id):
            outcome = original_persist(item, product_id=product_id)
            first_persisted.set()
            return outcome

        async def interrupted_fetch(
            targets,
            concurrency,
            minimum=1,
            result_callback=None,
            stop_event=None,
        ):
            result_callback(
                BarcodeFetchResult(
                    offer_id=completed_offer.pk,
                    ean=VALID_EAN,
                    source="jsonld_gtin",
                )
            )
            deadline = asyncio.get_running_loop().time() + 2
            while not first_persisted.is_set():
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("Completed EAN was not persisted")
                await asyncio.sleep(0.01)
            raise KeyboardInterrupt

        with (
            patch(
                "parsers.services.bauhaus_barcode_enricher._fetch_barcodes",
                interrupted_fetch,
            ),
            patch(
                "parsers.services.bauhaus_barcode_enricher._persist_fetch_result",
                side_effect=tracking_persist,
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            enrich_bauhaus_offer_barcodes(
                [completed_offer.pk, pending_offer.pk],
            )

        completed_offer.refresh_from_db()
        pending_offer.refresh_from_db()
        self.assertEqual(completed_offer.barcode, VALID_EAN)
        self.assertIsNotNone(completed_offer.barcode_checked_at)
        self.assertEqual(pending_offer.barcode, "")
        self.assertIsNone(pending_offer.barcode_checked_at)

    def test_progress_is_logged_before_download_finishes(self):
        first_offer = self.create_offer("SKU-FIRST")
        second_offer = self.create_offer("SKU-SECOND")
        progress_logged = threading.Event()
        logs = []

        def log_callback(message):
            logs.append(message)
            if "barcode progress" in message and "checked=1/2" in message:
                progress_logged.set()

        async def fake_fetch(
            targets,
            concurrency,
            minimum=1,
            result_callback=None,
            stop_event=None,
        ):
            result_callback(
                BarcodeFetchResult(
                    offer_id=first_offer.pk,
                    ean=VALID_EAN,
                    source="jsonld_gtin",
                )
            )
            deadline = asyncio.get_running_loop().time() + 2
            while not progress_logged.is_set():
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("Progress was not logged during download")
                await asyncio.sleep(0.01)
            result_callback(
                BarcodeFetchResult(
                    offer_id=second_offer.pk,
                    ean=VALID_EAN,
                    source="jsonld_gtin",
                )
            )
            return []

        with (
            patch(
                "parsers.services.bauhaus_barcode_enricher._fetch_barcodes",
                fake_fetch,
            ),
            patch(
                "parsers.services.bauhaus_barcode_enricher.PROGRESS_EVERY_ITEMS",
                1,
            ),
        ):
            enrich_bauhaus_offer_barcodes(
                [first_offer.pk, second_offer.pk],
                log_callback=log_callback,
            )

        self.assertTrue(progress_logged.is_set())
        self.assertTrue(any("remaining=1" in message for message in logs))

    def test_existing_barcode_and_checked_offer_are_skipped(self):
        barcode_offer = self.create_offer("SKU-BARCODE", barcode=VALID_EAN)
        checked_offer = self.create_offer("SKU-CHECKED", checked_at=timezone.now())

        with patch("parsers.services.bauhaus_barcode_enricher._fetch_barcodes") as fetch:
            result = enrich_bauhaus_offer_barcodes([barcode_offer.pk, checked_offer.pk])

        self.assertEqual(result.checked, 0)
        fetch.assert_not_called()

    def test_related_product_data_fills_multiple_exact_targets_with_one_page(self):
        second_ean = "9780201379624"
        document = (
            '{"hits":['
            '{"sku":"SKU-A","ean":"4006381333931",'
            '"canonical_url":"/variant-a"},'
            '{"sku":"SKU-B","ean":"9780201379624",'
            '"canonical_url":"/variant-b"}'
            "]}"
        )
        targets = [
            BarcodeTarget(1, "SKU-A", f"https://www.bauhaus.ee/variant-a", (
                "SKU-A",
                "https://www.bauhaus.ee/variant-a",
            )),
            BarcodeTarget(2, "SKU-B", f"https://www.bauhaus.ee/variant-b", (
                "SKU-B",
                "https://www.bauhaus.ee/variant-b",
            )),
        ]
        calls = []

        async def fake_page(*args, **kwargs):
            calls.append(args[4])
            return ProductPageResult(
                document=document,
                source="html",
                resolved_url=args[4],
                requests=1,
                deployment_id="dpl_test",
            )

        with patch(
            "parsers.services.bauhaus_barcode_client.fetch_product_page",
            fake_page,
        ):
            items = asyncio.run(_fetch_barcodes(targets, concurrency=2))

        fetched = {
            item.offer_id: item.ean
            for item in items
            if isinstance(item, BarcodeFetchResult)
        }
        self.assertEqual(calls, ["https://www.bauhaus.ee/variant-a"])
        self.assertEqual(fetched, {1: VALID_EAN, 2: second_ean})

    def test_same_sku_different_urls_are_fetched_and_assigned_separately(self):
        second_ean = "9780201379624"
        targets = [
            BarcodeTarget(1, "SAME", "https://www.bauhaus.ee/a", (
                "SAME",
                "https://www.bauhaus.ee/a",
            )),
            BarcodeTarget(2, "SAME", "https://www.bauhaus.ee/b", (
                "SAME",
                "https://www.bauhaus.ee/b",
            )),
        ]
        calls = []

        async def fake_page(*args, **kwargs):
            product_url = args[4]
            calls.append(product_url)
            ean = VALID_EAN if product_url.endswith("/a") else second_ean
            return ProductPageResult(
                document=(
                    '<script type="application/ld+json">'
                    f'{{"@type":"Product","sku":"SAME","gtin13":"{ean}"}}'
                    "</script>"
                ),
                source="html",
                resolved_url=product_url,
                requests=1,
                deployment_id="dpl_test",
            )

        with patch(
            "parsers.services.bauhaus_barcode_client.fetch_product_page",
            fake_page,
        ):
            items = asyncio.run(_fetch_barcodes(targets, concurrency=2))

        fetched = {
            item.offer_id: item.ean
            for item in items
            if isinstance(item, BarcodeFetchResult)
        }
        self.assertCountEqual(
            calls,
            ["https://www.bauhaus.ee/a", "https://www.bauhaus.ee/b"],
        )
        self.assertEqual(fetched, {1: VALID_EAN, 2: second_ean})

    def test_related_extraction_uses_sku_and_normalized_url_key(self):
        records = extract_related_product_eans(
            '{"hits":['
            '{"sku":"SAME","ean":"4006381333931","canonical_url":"/a"},'
            '{"sku":"SAME","ean":"9780201379624","canonical_url":"/b"}'
            "]}"
        )

        self.assertEqual(
            records[("SAME", "https://www.bauhaus.ee/a")],
            VALID_EAN,
        )
        self.assertEqual(
            records[("SAME", "https://www.bauhaus.ee/b")],
            "9780201379624",
        )

    def test_restriction_reduces_actual_limiter_from_maximum(self):
        events = []
        limiter = AdjustableLimiter(200)
        controller = FastAdaptiveController(
            limiter,
            minimum=8,
            reduction_factor=0.70,
            pause_seconds=0,
            event_callback=events.append,
        )

        asyncio.run(controller.report_restriction("HTTP 429", retry_after=0))
        controller.finish()

        self.assertEqual(limiter.limit, 140)
        self.assertTrue(events[0].persist_tuning)
        self.assertEqual(events[0].concurrency, 140)
        self.assertTrue(events[-1].stable)

    def test_rsc_is_used_when_deployment_id_is_known(self):
        class Response:
            status_code = 200
            text = (
                '{"hits":[{"sku":"SKU-A","ean":"4006381333931",'
                '"canonical_url":"/a"}]}'
            )
            url = "https://www.bauhaus.ee/a?_rsc=test"
            headers = {}

        class Session:
            def __init__(self):
                self.calls = []

            async def get(self, url, **kwargs):
                self.calls.append((url, kwargs["headers"]))
                return Response()

        async def run():
            session = Session()
            limiter = AdjustableLimiter(10)
            controller = FastAdaptiveController(
                limiter,
                minimum=1,
                reduction_factor=0.7,
                pause_seconds=0,
            )
            result = await _fetch_product_page(
                session,
                limiter,
                controller,
                "SKU-A",
                "https://www.bauhaus.ee/a",
                deployment_id="dpl_test",
            )
            return session, result

        session, result = asyncio.run(run())

        self.assertEqual(result.source, "rsc")
        self.assertEqual(result.requests, 1)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][1]["rsc"], "1")
        self.assertIn("_rsc=", session.calls[0][0])

    def test_invalid_rsc_falls_back_to_full_html(self):
        class Response:
            def __init__(self, status, text, url):
                self.status_code = status
                self.text = text
                self.url = url
                self.headers = {}

        class Session:
            def __init__(self):
                self.calls = []
                self.responses = [
                    Response(404, "", "https://www.bauhaus.ee/a?_rsc=test"),
                    Response(
                        200,
                        '<script type="application/ld+json">'
                        '{"@type":"Product","sku":"SKU-A",'
                        '"gtin13":"4006381333931"}'
                        "</script>dpl_new",
                        "https://www.bauhaus.ee/a",
                    ),
                ]

            async def get(self, url, **kwargs):
                self.calls.append(url)
                return self.responses.pop(0)

        async def run():
            session = Session()
            limiter = AdjustableLimiter(10)
            controller = FastAdaptiveController(
                limiter,
                minimum=1,
                reduction_factor=0.7,
                pause_seconds=0,
            )
            result = await _fetch_product_page(
                session,
                limiter,
                controller,
                "SKU-A",
                "https://www.bauhaus.ee/a",
                deployment_id="dpl_old",
            )
            return session, result

        session, result = asyncio.run(run())

        self.assertEqual(result.source, "html")
        self.assertEqual(result.requests, 2)
        self.assertEqual(len(session.calls), 2)

    def test_saved_concurrency_is_reused(self):
        offer = self.create_offer("SKU-SAVED")
        ParserConfig.objects.create(
            shop=self.shop,
            name="BAUHAUS",
            code="bauhaus",
            runtime_settings={
                "bauhaus_barcode_enrichment": {"concurrency": 37},
            },
        )
        captured = {}

        async def fake_fetch(
            targets,
            concurrency,
            minimum=1,
            result_callback=None,
            stop_event=None,
        ):
            captured["concurrency"] = concurrency
            result_callback(
                BarcodeFetchResult(
                    offer_id=offer.pk,
                    ean=VALID_EAN,
                    source="html_jsonld",
                )
            )
            return []

        with patch(
            "parsers.services.bauhaus_barcode_enricher._fetch_barcodes",
            fake_fetch,
        ):
            result = enrich_bauhaus_offer_barcodes([offer.pk])

        self.assertEqual(captured["concurrency"], 37)
        self.assertEqual(result.start_concurrency, 37)

    def test_adapted_concurrency_is_persisted(self):
        offer = self.create_offer("SKU-TUNED")
        parser_config = ParserConfig.objects.create(
            shop=self.shop,
            name="BAUHAUS",
            code="bauhaus",
        )

        async def fake_fetch(
            targets,
            concurrency,
            minimum=1,
            result_callback=None,
            stop_event=None,
        ):
            result_callback(
                BarcodeFetchMetrics(
                    restrictions=3,
                    concurrency=42,
                    persist_tuning=True,
                    stable=True,
                )
            )
            result_callback(
                BarcodeFetchResult(
                    offer_id=offer.pk,
                    ean=VALID_EAN,
                    source="rsc_product_data",
                )
            )
            return []

        with patch(
            "parsers.services.bauhaus_barcode_enricher._fetch_barcodes",
            fake_fetch,
        ):
            enrich_bauhaus_offer_barcodes([offer.pk], retune=True)

        parser_config.refresh_from_db()
        tuning = parser_config.runtime_settings["bauhaus_barcode_enrichment"]
        self.assertEqual(tuning["concurrency"], 42)
        self.assertEqual(tuning["restrictions"], 3)
        self.assertTrue(tuning["stable"])


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
