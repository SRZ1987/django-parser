import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from django.test import TestCase

from catalog.models import PriceHistory, Product, ProductOffer, Shop
from parsers.models import ParserConfig, ParserRun
from parsers.services.depo_client import DepoClient, ROWS
from parsers.services.runner import run_parser


def depo_product(
    product_id="101",
    name="DEPO drill",
    price=Decimal("12.99"),
    sale_price=None,
    barcode="EAN-101",
    quantity_price=None,
    quantity_price_min_quantity=None,
):
    return {
        "id": product_id,
        "name": name,
        "price": price,
        "sale_price": sale_price,
        "quantity_price": quantity_price,
        "quantity_price_min_quantity": quantity_price_min_quantity,
        "barcode": barcode,
        "sku": product_id,
        "image_url": f"https://online.depo.ee/images/{product_id}.jpg",
        "product_url": f"https://online.depo.ee/product/{product_id}",
        "category_id": "7",
    }


class DepoParserTests(TestCase):
    def setUp(self):
        self.depo_shop = Shop.objects.create(name="DEPO", code="depo")
        self.depo_config = ParserConfig.objects.create(
            shop=self.depo_shop,
            name="DEPO parser",
            code="depo",
        )
        self.espak_shop = Shop.objects.create(name="ESPAK", code="espak")
        self.categories = [{"id": 7}]

    def run_with_products(self, products):
        with patch(
            "parsers.services.depo.DepoParser._fetch_remote_data",
            new=AsyncMock(return_value=(self.categories, products)),
        ):
            return run_parser("depo")

    def create_depo_offer(self, external_id="101", name="Old DEPO drill", is_active=True, is_available=True):
        product = Product.objects.create(name=name)
        return ProductOffer.objects.create(
            shop=self.depo_shop,
            product=product,
            external_id=external_id,
            sku=external_id,
            original_name=name,
            price=Decimal("10.00"),
            is_active=is_active,
            is_available=is_available,
        )

    def test_creates_new_offer(self):
        parser_run = self.run_with_products([depo_product()])

        self.assertEqual(parser_run.status, ParserRun.STATUS_SUCCESS)
        self.assertEqual(parser_run.products_created, 1)
        self.assertEqual(ProductOffer.objects.filter(shop=self.depo_shop).count(), 1)
        self.assertEqual(Product.objects.count(), 1)

    def test_updates_existing_offer(self):
        self.run_with_products([depo_product()])

        parser_run = self.run_with_products([depo_product(name="Updated DEPO drill")])

        self.assertEqual(parser_run.products_updated, 1)
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(ProductOffer.objects.get(shop=self.depo_shop, external_id="101").original_name, "Updated DEPO drill")

    def test_price_change_creates_price_history(self):
        self.run_with_products([depo_product(price=Decimal("12.99"))])

        parser_run = self.run_with_products([depo_product(price=Decimal("14.99"))])

        self.assertEqual(parser_run.prices_changed, 1)
        self.assertEqual(PriceHistory.objects.count(), 2)

    def test_quantity_price_is_saved_separately_from_sale_price(self):
        parser_run = self.run_with_products(
            [
                depo_product(
                    quantity_price=Decimal("9.99"),
                    quantity_price_min_quantity=6,
                )
            ]
        )

        offer = ProductOffer.objects.get(shop=self.depo_shop, external_id="101")
        self.assertEqual(parser_run.status, ParserRun.STATUS_SUCCESS)
        self.assertIsNone(offer.sale_price)
        self.assertEqual(offer.quantity_price, Decimal("9.99"))
        self.assertEqual(offer.quantity_price_min_quantity, 6)
        history = PriceHistory.objects.get(offer=offer)
        self.assertEqual(history.quantity_price, Decimal("9.99"))
        self.assertEqual(history.quantity_price_min_quantity, 6)

    def test_same_price_does_not_create_price_history(self):
        self.run_with_products([depo_product(price=Decimal("12.99"))])

        parser_run = self.run_with_products([depo_product(price=Decimal("12.99"))])

        self.assertEqual(parser_run.prices_changed, 0)
        self.assertEqual(PriceHistory.objects.count(), 1)

    def test_safe_deactivation(self):
        self.run_with_products([depo_product(product_id="101")])

        parser_run = self.run_with_products([depo_product(product_id="202")])

        self.assertEqual(parser_run.status, ParserRun.STATUS_SUCCESS)
        old_offer = ProductOffer.objects.get(shop=self.depo_shop, external_id="101")
        self.assertFalse(old_offer.is_active)
        self.assertFalse(old_offer.is_available)

    def test_duplicates_are_not_saved_twice(self):
        parser_run = self.run_with_products([depo_product(product_id="101"), depo_product(product_id="101")])

        self.assertEqual(parser_run.products_found, 1)
        self.assertEqual(ProductOffer.objects.filter(shop=self.depo_shop).count(), 1)

    def test_network_error_fails_without_deactivation(self):
        self.create_depo_offer(external_id="101")

        with patch(
            "parsers.services.depo.DepoParser._fetch_remote_data",
            new=AsyncMock(side_effect=RuntimeError("network down")),
        ):
            parser_run = run_parser("depo")

        self.assertEqual(parser_run.status, ParserRun.STATUS_FAILED)
        offer = ProductOffer.objects.get(shop=self.depo_shop, external_id="101")
        self.assertTrue(offer.is_active)
        self.assertTrue(offer.is_available)

    def test_empty_catalog_fails_without_deactivation(self):
        self.create_depo_offer(external_id="101")

        parser_run = self.run_with_products([])

        self.assertEqual(parser_run.status, ParserRun.STATUS_FAILED)
        offer = ProductOffer.objects.get(shop=self.depo_shop, external_id="101")
        self.assertTrue(offer.is_active)
        self.assertTrue(offer.is_available)

    def test_small_catalog_fails_without_deactivation(self):
        for index in range(100):
            self.create_depo_offer(external_id=f"old-{index}")

        parser_run = self.run_with_products([depo_product(product_id=str(index)) for index in range(10)])

        self.assertEqual(parser_run.status, ParserRun.STATUS_FAILED)
        self.assertIn("anomalously small product list", parser_run.error_message)
        self.assertEqual(
            ProductOffer.objects.filter(shop=self.depo_shop, is_active=True, is_available=True).count(),
            100,
        )

    def test_espak_offers_are_not_touched(self):
        espak_product = Product.objects.create(name="ESPAK product")
        espak_offer = ProductOffer.objects.create(
            shop=self.espak_shop,
            product=espak_product,
            external_id="espak-1",
            original_name="ESPAK product",
            is_active=True,
            is_available=True,
        )

        self.run_with_products([depo_product(product_id="101")])

        espak_offer.refresh_from_db()
        self.assertTrue(espak_offer.is_active)
        self.assertTrue(espak_offer.is_available)


class DepoClientQueueTests(TestCase):
    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    def test_worker_calls_task_done_even_when_page_fails(self):
        async def scenario():
            client = DepoClient()
            queue = asyncio.Queue()
            errors = []
            await queue.put((1, ROWS))
            client._get_products_page = AsyncMock(side_effect=RuntimeError("page failed"))
            worker = asyncio.create_task(client._worker(1, None, queue, {}, errors))

            await asyncio.wait_for(queue.join(), timeout=1)
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

            self.assertEqual(queue._unfinished_tasks, 0)
            self.assertEqual(len(errors), 1)

        self.run_async(scenario())

    def test_worker_finishes_when_cancelled_after_queue_join(self):
        async def scenario():
            client = DepoClient()
            queue = asyncio.Queue()
            products = {}
            errors = []
            worker = asyncio.create_task(client._worker(1, None, queue, products, errors))
            await queue.put((1, ROWS))
            client._get_products_page = AsyncMock(return_value=(40, [depo_product(product_id="202")]))

            await asyncio.wait_for(queue.join(), timeout=1)
            worker.cancel()
            result = await asyncio.gather(worker, return_exceptions=True)

            self.assertIsInstance(result[0], asyncio.CancelledError)
            self.assertFalse(errors)

        self.run_async(scenario())

    def test_fetch_products_join_does_not_hang_when_worker_raises(self):
        async def scenario():
            client = DepoClient(progress_log_interval=0.01, watchdog_timeout=1)

            async def fake_page(session, category_id, start):
                if start == 0:
                    return ROWS * 2, [depo_product(product_id="101")]
                raise RuntimeError("worker page failed")

            client._get_session = AsyncMock(return_value=None)
            client._get_products_page = AsyncMock(side_effect=fake_page)

            with self.assertRaisesMessage(RuntimeError, "DEPO product pagination failed"):
                await asyncio.wait_for(client.fetch_products([{"id": 1}]), timeout=1)

        self.run_async(scenario())

    def test_worker_errors_make_parser_run_failed_without_deactivation(self):
        shop = Shop.objects.create(name="DEPO", code="depo")
        ParserConfig.objects.create(shop=shop, name="DEPO parser", code="depo")
        product = Product.objects.create(name="Old DEPO")
        offer = ProductOffer.objects.create(
            shop=shop,
            product=product,
            external_id="101",
            original_name="Old DEPO",
            is_active=True,
            is_available=True,
        )

        with patch(
            "parsers.services.depo.DepoParser._fetch_remote_data",
            new=AsyncMock(side_effect=RuntimeError("DEPO product pagination failed: page failed")),
        ):
            parser_run = run_parser("depo")

        offer.refresh_from_db()
        self.assertEqual(parser_run.status, ParserRun.STATUS_FAILED)
        self.assertTrue(offer.is_active)
        self.assertTrue(offer.is_available)

    def test_progress_is_logged_during_fetch(self):
        async def scenario():
            logs = []
            client = DepoClient(log_callback=logs.append, progress_log_interval=0.01, watchdog_timeout=1)

            async def fake_page(session, category_id, start):
                if start == 0:
                    return ROWS * 2, [depo_product(product_id="101")]
                await asyncio.sleep(0.05)
                return ROWS * 2, [depo_product(product_id="202", barcode="EAN-202")]

            client._get_session = AsyncMock(return_value=None)
            client._get_products_page = AsyncMock(side_effect=fake_page)

            products = await client.fetch_products([{"id": 1}])

            self.assertEqual(len(products), 2)
            self.assertTrue(any("DEPO download progress" in message for message in logs))

        self.run_async(scenario())

    def test_watchdog_stops_stalled_import(self):
        async def scenario():
            client = DepoClient(progress_log_interval=0.01, watchdog_timeout=0.02)

            async def fake_page(session, category_id, start):
                if start == 0:
                    return ROWS * 2, [depo_product(product_id="101")]
                await asyncio.sleep(1)
                return ROWS * 2, [depo_product(product_id="202")]

            client._get_session = AsyncMock(return_value=None)
            client._get_products_page = AsyncMock(side_effect=fake_page)

            with self.assertRaisesMessage(RuntimeError, "DEPO download stalled"):
                await client.fetch_products([{"id": 1}])

        self.run_async(scenario())
