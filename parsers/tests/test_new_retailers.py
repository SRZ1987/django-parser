import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase

from parsers.adapters.lemona import LemonaAdapter
from parsers.adapters.motonet import MotonetAdapter
from parsers.adapters.oomipood import OomipoodAdapter
from parsers.adapters.registry import ADAPTERS
from parsers.adapters.sitemap_retailers import EffexAdapter, VipexAdapter
from parsers.standalone import (
    lemona_parser,
    motonet_parser,
    oomipood_parser,
    public_commerce_parser,
    sitemap_retailers_parser,
)


OOMIPOOD_HTML = """
<!doctype html>
<html>
  <head>
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Test power supply",
        "sku": "PSU-100",
        "image": "https://img.test/psu.jpg",
        "description": "Stable laboratory power supply",
        "offers": {
          "@type": "Offer",
          "priceCurrency": "EUR",
          "price": 12.50,
          "availability": "https://schema.org/InStock"
        }
      }
    </script>
  </head>
  <body class="product-product-1234">
    <nav class="breadcrumb">
      <a href="https://www.oomipood.ee/category/power">Power supplies</a>
    </nav>
    <span class="price-old">15,00 EUR</span>
    <span class="price-new">12,50 EUR</span>
    <div id="tab-tech"><b>SKU:</b> PSU-100<br><b>EAN:</b> 4740000000001</div>
  </body>
</html>
"""


VIPEX_HTML = """
<!doctype html>
<html>
  <head>
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
          {"@type": "ListItem", "position": 1, "item": {"name": "Home", "@id": "https://vipex.test/"}},
          {"@type": "ListItem", "position": 2, "item": {"name": "Tiles", "@id": "https://vipex.test/tiles"}}
        ]
      }
    </script>
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Ceramic wall tile",
        "sku": "V0392011",
        "image": "https://img.test/tile.jpg",
        "description": "Durable ceramic tile",
        "offers": {
          "@type": "Offer",
          "price": "15.50",
          "priceCurrency": "EUR",
          "availability": "https://schema.org/InStock"
        }
      }
    </script>
  </head>
  <body class="catalog-product-view catalog-product-view-id-3 catalog_product_view_id_3">
    <span class="old-price"><span data-price-amount="19.50">19.50</span></span>
  </body>
</html>
"""


EFFEX_HTML = """
<!doctype html>
<html>
  <head>
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "LED spots",
        "description": "Efficient LED spot family",
        "category": "LED GU10",
        "image": "https://img.test/spot.jpg",
        "offers": {
          "@type": "Offer",
          "price": "2.82",
          "priceCurrency": "EUR",
          "availability": "https://schema.org/InStock"
        }
      }
    </script>
  </head>
  <body>
    <div class="variations visible--desktop">
      <table><tbody>
        <tr class="product-row outlet">
          <td>8718696686904 <br><span>Outlet</span></td>
          <td>LEDspot 5W 550lm GU10</td>
          <td><button data-attribute="1787">Availability</button></td>
          <td><span class="base-price">3,50</span><br><strong>2,28</strong></td>
          <td><span class="discount">-35%</span><br><strong>2,82</strong></td>
        </tr>
        <tr class="product-row">
          <td>8719514308657</td>
          <td>LEDspot 4.9W 550lm GU10</td>
          <td><button data-attribute="15667">Availability</button></td>
          <td><strong>3,20</strong></td>
          <td><strong>3,97</strong></td>
        </tr>
      </tbody></table>
    </div>
  </body>
</html>
"""


MOTONET_PRODUCT = {
    "id": "45-7004",
    "name": "Fuel and oil vacuum hose 4/9 mm",
    "description": "Multi-purpose fuel hose",
    "price": "4,99",
    "brand": "Gates",
    "categoryName": "Hoses and clamps",
    "categoryUrl": "/tooteruhmad/car-accessories/hoses?category=category-123",
}


def lemona_product(**overrides):
    product = {
        "id": 101,
        "sku": "430702",
        "lemona_sku": "HA-GREEN",
        "title": "Home Assistant Green",
        "price": 90,
        "old_price": 100,
        "stock_status": "IN_STOCK",
        "image_url": "media/catalog/product/ha-green.jpg",
        "url": "/home-assistant-green.html",
        "description": "Smart home controller",
        "category_id": 12,
        "category_ids": [4, 12],
        "categories": ["Smart home", "Controllers"],
    }
    product.update(overrides)
    return product


def lemona_detail(**overrides):
    detail = {
        "sku": "430702",
        "lemona_sku": "HA-GREEN",
        "bkodai": "860011789703; 860011789704",
        "price_tiers": [
            {
                "quantity": 5,
                "final_price": {"value": 80, "currency": "EUR"},
            }
        ],
    }
    detail.update(overrides)
    return detail


class OomipoodParserTests(SimpleTestCase):
    def test_product_page_extracts_sale_price_ean_and_stable_id(self):
        row = oomipood_parser.parse_product_page(
            OOMIPOOD_HTML,
            "https://www.oomipood.ee/product/test-power-supply",
        )

        self.assertEqual(row[0], "Test power supply")
        self.assertEqual(row[1], 15.0)
        self.assertEqual(row[2], 12.5)
        self.assertEqual(row[4], "4740000000001")
        self.assertEqual(row[5], "oomipood-1234")
        self.assertEqual(row[8], "PSU-100")
        self.assertEqual(row[9], "Power supplies")

    def test_out_of_stock_product_is_skipped(self):
        html = OOMIPOOD_HTML.replace("schema.org/InStock", "schema.org/OutOfStock")

        self.assertIsNone(
            oomipood_parser.parse_product_page(
                html,
                "https://www.oomipood.ee/product/test-power-supply",
            )
        )

    def test_excel_forbidden_control_character_is_removed(self):
        html = OOMIPOOD_HTML.replace(
            "Stable laboratory power supply",
            r"Stable\b laboratory power supply",
        )
        row = oomipood_parser.parse_product_page(
            html,
            "https://www.oomipood.ee/product/test-power-supply",
        )

        self.assertEqual(row[11], "Stable laboratory power supply")
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "oomipood.xlsx"
            public_commerce_parser.save_excel([row], output_path)
            self.assertTrue(output_path.exists())

    def test_adapter_creates_excel_and_counts_rows(self):
        async def fake_main(output_path, log_callback=None):
            row = oomipood_parser.parse_product_page(
                OOMIPOOD_HTML,
                "https://www.oomipood.ee/product/test-power-supply",
            )
            public_commerce_parser.save_excel([row], Path(output_path))
            log_callback("Oomipood progress")

        logs = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "oomipood.xlsx"
            with patch("parsers.adapters.oomipood.oomipood_parser.main", fake_main):
                result = asyncio.run(OomipoodAdapter().run(output_path, log_callback=logs.append))

        self.assertTrue(result.success)
        self.assertEqual(result.products_count, 1)
        self.assertEqual(logs, ["Oomipood progress"])


class LemonaParserTests(SimpleTestCase):
    def test_catalog_request_uses_stable_sku_sort(self):
        self.assertEqual(
            lemona_parser.lupa_request(0)["sort"],
            [{"sku": "asc"}],
        )

    def test_product_uses_public_sku_price_barcode_and_quantity_tier(self):
        rows, skipped = lemona_parser.build_rows(
            [lemona_product()],
            {"430702": lemona_detail()},
        )

        self.assertEqual(skipped, 0)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row[0], "Home Assistant Green")
        self.assertEqual(row[1], 100.0)
        self.assertEqual(row[2], 90.0)
        self.assertEqual(row[3], 80.0)
        self.assertEqual(row[4], "860011789703")
        self.assertEqual(row[5], "lemona-430702")
        self.assertEqual(row[8], "HA-GREEN")
        self.assertEqual(row[9], "Controllers")
        self.assertEqual(row[12], 5)

    def test_quantity_tier_above_current_sale_price_is_ignored(self):
        rows, _skipped = lemona_parser.build_rows(
            [lemona_product(price=56.5, old_price=84)],
            {"430702": lemona_detail(price_tiers=[{"quantity": 2, "final_price": {"value": 79.8}}])},
        )

        self.assertEqual(rows[0][1], 84.0)
        self.assertEqual(rows[0][2], 56.5)
        self.assertEqual(rows[0][3], "")
        self.assertEqual(rows[0][12], "")

    def test_adapter_creates_excel_and_counts_rows(self):
        async def fake_main(output_path, log_callback=None):
            rows, _skipped = lemona_parser.build_rows(
                [lemona_product()],
                {"430702": lemona_detail()},
            )
            public_commerce_parser.save_excel(
                rows,
                Path(output_path),
                columns=lemona_parser.COLUMNS,
            )
            log_callback("Lemona progress")

        logs = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "lemona.xlsx"
            with patch("parsers.adapters.lemona.lemona_parser.main", fake_main):
                result = asyncio.run(LemonaAdapter().run(output_path, log_callback=logs.append))

        self.assertTrue(result.success)
        self.assertEqual(result.products_count, 1)
        self.assertEqual(logs, ["Lemona progress"])


class SitemapRetailerParserTests(SimpleTestCase):
    def test_sitemap_keeps_only_entries_with_product_images(self):
        sitemap = """
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
          <url><loc>https://shop.test/category</loc></url>
          <url>
            <loc>https://shop.test/product</loc>
            <image:image><image:loc>https://img.test/product.jpg</image:loc></image:image>
          </url>
        </urlset>
        """

        self.assertEqual(
            sitemap_retailers_parser.parse_sitemap_products(sitemap),
            [("https://shop.test/product", "https://img.test/product.jpg")],
        )

    def test_vipex_product_extracts_price_sale_sku_and_category(self):
        rows = sitemap_retailers_parser.parse_vipex_page(
            VIPEX_HTML,
            "https://www.vipex.ee/ceramic-wall-tile",
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row[0], "Ceramic wall tile")
        self.assertEqual(row[1], 19.5)
        self.assertEqual(row[2], 15.5)
        self.assertEqual(row[5], "vipex-3")
        self.assertEqual(row[8], "V0392011")
        self.assertEqual(row[9], "Tiles")

    def test_vipex_out_of_stock_product_is_skipped(self):
        html = VIPEX_HTML.replace("schema.org/InStock", "schema.org/OutOfStock")

        self.assertEqual(
            sitemap_retailers_parser.parse_vipex_page(
                html,
                "https://www.vipex.ee/ceramic-wall-tile",
            ),
            [],
        )

    def test_product_redirect_to_store_home_is_treated_as_removed(self):
        self.assertTrue(
            sitemap_retailers_parser.redirected_to_store_home(
                "https://www.vipex.ee/removed-product",
                "https://www.vipex.ee/",
                "https://www.vipex.ee/",
            )
        )
        self.assertFalse(
            sitemap_retailers_parser.redirected_to_store_home(
                "https://www.vipex.ee/available-product",
                "https://www.vipex.ee/available-product",
                "https://www.vipex.ee/",
            )
        )

    def test_effex_variants_keep_separate_ids_barcodes_and_prices(self):
        rows = sitemap_retailers_parser.parse_effex_page(
            EFFEX_HTML,
            "https://effex.ee/et/led-gu10/11-led-spots.html",
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "LEDspot 5W 550lm GU10")
        self.assertEqual(rows[0][1], 4.33)
        self.assertEqual(rows[0][2], 2.82)
        self.assertEqual(rows[0][4], "8718696686904")
        self.assertEqual(rows[0][5], "effex-11-1787")
        self.assertEqual(rows[1][1], 3.97)
        self.assertEqual(rows[1][2], "")
        self.assertEqual(rows[1][4], "8719514308657")
        self.assertEqual(rows[1][5], "effex-11-15667")

    def test_missing_product_markup_is_retried_before_failing_catalog(self):
        calls = 0

        async def fake_request_text(*args, **kwargs):
            nonlocal calls
            calls += 1
            return "<html><body>No product markup</body></html>" if calls == 1 else EFFEX_HTML

        logs = []
        store = sitemap_retailers_parser.SITEMAP_RETAILERS["effex"]
        with (
            patch.object(sitemap_retailers_parser, "request_text", fake_request_text),
            patch.object(sitemap_retailers_parser.asyncio, "sleep", AsyncMock()),
        ):
            rows, skipped = asyncio.run(
                sitemap_retailers_parser.fetch_rows(
                    None,
                    store,
                    [("https://effex.ee/et/led-gu10/11-led-spots.html", "")],
                    logs.append,
                )
            )

        self.assertEqual(calls, 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(skipped, 0)
        self.assertTrue(any("product JSON-LD is missing" in entry for entry in logs))

    def test_persistently_missing_product_markup_fails_catalog(self):
        async def fake_request_text(*args, **kwargs):
            return "<html><body>No product markup</body></html>"

        store = sitemap_retailers_parser.SITEMAP_RETAILERS["effex"]
        with (
            patch.object(sitemap_retailers_parser, "request_text", fake_request_text),
            patch.object(sitemap_retailers_parser.asyncio, "sleep", AsyncMock()),
        ):
            with self.assertRaises(sitemap_retailers_parser.ProductMarkupMissing):
                asyncio.run(
                    sitemap_retailers_parser.fetch_rows(
                        None,
                        store,
                        [("https://effex.ee/et/led-gu10/missing.html", "")],
                    )
                )

    def test_sitemap_adapters_create_excel_and_count_rows(self):
        async def fake_main(store_code, output_path, log_callback=None):
            if store_code == "vipex":
                rows = sitemap_retailers_parser.parse_vipex_page(
                    VIPEX_HTML,
                    "https://www.vipex.ee/ceramic-wall-tile",
                )
            else:
                rows = sitemap_retailers_parser.parse_effex_page(
                    EFFEX_HTML,
                    "https://effex.ee/et/led-gu10/11-led-spots.html",
                )
            public_commerce_parser.save_excel(rows, Path(output_path))
            log_callback(f"{store_code} progress")

        with patch(
            "parsers.adapters.sitemap_retailers.sitemap_retailers_parser.main",
            fake_main,
        ):
            for adapter_class, expected_count in ((VipexAdapter, 1), (EffexAdapter, 2)):
                with self.subTest(adapter=adapter_class.code), tempfile.TemporaryDirectory() as tmp_dir:
                    logs = []
                    output_path = Path(tmp_dir) / f"{adapter_class.code}.xlsx"
                    result = asyncio.run(
                        adapter_class().run(output_path, log_callback=logs.append)
                    )

                    self.assertTrue(result.success)
                    self.assertEqual(result.products_count, expected_count)
                    self.assertEqual(logs, [f"{adapter_class.code} progress"])


class MotonetParserTests(SimpleTestCase):
    def test_category_sitemap_keeps_only_top_level_categories(self):
        sitemap = """
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.motonet.ee/tooteruhmad/tools?category=top-1</loc></url>
          <url><loc>https://www.motonet.ee/tooteruhmad/tools/drills?category=child-1</loc></url>
          <url><loc>https://www.motonet.ee/contact</loc></url>
        </urlset>
        """

        self.assertEqual(
            motonet_parser.parse_top_categories(sitemap),
            [
                {
                    "id": "top-1",
                    "slug": "tools",
                    "url": "https://www.motonet.ee/tooteruhmad/tools?category=top-1",
                }
            ],
        )

    def test_available_product_uses_batch_price_and_stable_sku(self):
        availability = {
            "45-7004": {
                "productCode": "45-7004",
                "webstoreDeliverable": True,
                "locations": [],
            }
        }
        prices = {
            "45-7004": {
                "product": {"productCode": "45-7004"},
                "price": {"price": 4.99},
                "campaign": None,
            }
        }

        rows, skipped = motonet_parser.build_rows(
            [MOTONET_PRODUCT],
            availability,
            prices,
        )

        self.assertEqual(skipped, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Fuel and oil vacuum hose 4/9 mm")
        self.assertEqual(rows[0][1], 4.99)
        self.assertEqual(rows[0][5], "motonet-45-7004")
        self.assertEqual(rows[0][8], "45-7004")
        self.assertEqual(rows[0][10], "motonet-category-category-123")

    def test_unavailable_product_is_skipped(self):
        availability = {
            "45-7004": {
                "productCode": "45-7004",
                "webstoreDeliverable": False,
                "orderableToLocations": False,
                "locations": [],
            }
        }

        rows, skipped = motonet_parser.build_rows(
            [MOTONET_PRODUCT],
            availability,
            {},
        )

        self.assertEqual(rows, [])
        self.assertEqual(skipped, 1)

    def test_campaign_price_is_saved_as_sale_price(self):
        price, sale_price = motonet_parser.extract_prices(
            {
                "price": {"price": 19.99},
                "campaign": {"mainCampaign": {"discountedPrice": 14.99}},
            }
        )

        self.assertEqual(price, 19.99)
        self.assertEqual(sale_price, 14.99)

    def test_missing_availability_is_retried_for_only_missing_products(self):
        logs = []
        initial_items = [{"productCode": "one", "webstoreDeliverable": True}]
        recovered_items = [{"productCode": "two", "webstoreDeliverable": True}]

        with patch(
            "parsers.standalone.motonet_parser.fetch_batch_data",
            new=AsyncMock(return_value=recovered_items),
        ) as fetch_mock:
            availability = asyncio.run(
                motonet_parser.recover_missing_availability(
                    None,
                    ["one", "two"],
                    initial_items,
                    log_callback=logs.append,
                )
            )

        self.assertEqual(set(availability), {"one", "two"})
        self.assertEqual(fetch_mock.await_args.args[1], ["two"])
        self.assertIn("retrying only the missing product codes", logs[0])

    def test_tiny_availability_gap_is_treated_as_unavailable(self):
        product_codes = [f"product-{index}" for index in range(3000)]
        availability = {
            product_code: {"productCode": product_code}
            for product_code in product_codes[:-2]
        }
        logs = []

        missing = motonet_parser.validate_availability_coverage(
            product_codes,
            availability,
            log_callback=logs.append,
        )

        self.assertEqual(missing, product_codes[-2:])
        self.assertIn("treating them as unavailable", logs[0])

    def test_significant_availability_gap_still_fails_import(self):
        product_codes = [f"product-{index}" for index in range(100)]
        availability = {
            product_code: {"productCode": product_code}
            for product_code in product_codes[:-2]
        }

        with self.assertRaisesRegex(RuntimeError, "response is incomplete"):
            motonet_parser.validate_availability_coverage(
                product_codes,
                availability,
            )

    def test_adapter_creates_excel_and_counts_rows(self):
        async def fake_main(output_path, log_callback=None):
            rows, _skipped = motonet_parser.build_rows(
                [MOTONET_PRODUCT],
                {
                    "45-7004": {
                        "productCode": "45-7004",
                        "webstoreDeliverable": True,
                    }
                },
                {
                    "45-7004": {
                        "product": {"productCode": "45-7004"},
                        "price": {"price": 4.99},
                    }
                },
            )
            public_commerce_parser.save_excel(rows, Path(output_path))
            log_callback("Motonet progress")

        logs = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "motonet.xlsx"
            with patch("parsers.adapters.motonet.motonet_parser.main", fake_main):
                result = asyncio.run(MotonetAdapter().run(output_path, log_callback=logs.append))

        self.assertTrue(result.success)
        self.assertEqual(result.products_count, 1)
        self.assertEqual(logs, ["Motonet progress"])


class NewRetailerRegistryTests(SimpleTestCase):
    def test_registry_contains_new_retailers(self):
        self.assertIn("tetko", ADAPTERS)
        self.assertIs(ADAPTERS["oomipood"], OomipoodAdapter)
        self.assertIs(ADAPTERS["lemona"], LemonaAdapter)
        self.assertIs(ADAPTERS["vipex"], VipexAdapter)
        self.assertIs(ADAPTERS["effex"], EffexAdapter)
        self.assertIs(ADAPTERS["motonet"], MotonetAdapter)
