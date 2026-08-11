import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from parsers.adapters.catalog_api_retailers import EsvikaAdapter, StokkerAdapter
from parsers.adapters.catalog_listing_retailers import ElektrikaupAdapter, HammerjackAdapter
from parsers.adapters.catalog_sitemap_retailers import ArcadeAdapter, ToruJyriAdapter
from parsers.adapters.registry import ADAPTERS, get_adapter_class
from parsers.services.excel_validation import ExcelCatalogValidator
from parsers.standalone import (
    catalog_api_retailers_parser,
    catalog_listing_retailers_parser,
    catalog_sitemap_retailers_parser,
)
from parsers.standalone.public_commerce_parser import save_excel


TORUJYRI_HTML = """
<html><head><script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [{
    "@type": "Product",
    "name": "Alupex pipe",
    "sku": "PIPE-20",
    "category": "Pipes &gt; Alupex",
    "description": "Composite pipe",
    "image": "https://img.test/pipe.jpg",
    "gtin13": "4740000000001",
    "offers": {
      "@type": "Offer",
      "price": "27.50",
      "availability": "https://schema.org/InStock"
    }
  }]
}
</script></head><body><h1 class="product_title">Alupex pipe 20 mm</h1></body></html>
"""


ARCADE_HTML = """
<html><body>
<ul class="breadcrumbs">
  <li class="home"><a href="/">Home</a></li>
  <li><a href="https://www.arcade.ee/pipes.html">Pipes</a></li>
</ul>
<form id="product_addtocart_form">
  <input name="product" value="32">
  <h1 class="product-name">Drain pipe connector</h1>
  <div class="price-box"><span class="regular-price"><span class="price">4,20 EUR</span></span></div>
  <p class="availability in-stock">In stock</p>
  <div class="short-description"><div class="std">Diameter 110 mm</div></div>
</form>
<img id="image" src="https://img.test/pipe.jpg">
</body></html>
"""


HAMMERJACK_HTML = """
<html><body>
<div class="header-menu"><div class="picture-title-wrap"><div class="title">
  <a href="/et/tools">Tools</a>
</div></div></div>
<div class="product-item" data-productid="99">
  <div class="picture"><img src="https://img.test/drill.jpg"></div>
  <div class="product-title"><a href="/et/test-drill">Test drill</a></div>
  <div class="sku">DRILL-99</div>
  <div class="description">Cordless drill</div>
  <div class="prices"><span class="old-price">120,00 EUR</span><span class="actual-price">99,00 EUR</span></div>
  <div class="stock-overview">In stock</div>
</div>
<div class="pager"><a href="/et/tools?pagesize=100&amp;pagenumber=3">Last</a></div>
</body></html>
"""


ELEKTRIKAUP_HTML = """
<html><body><div class="product">
  <div class="photo"><img src="https://img.test/lamp.jpg"></div>
  <div class="nameBlock">
    <a class="name" href="/lamps/10783/test-led-lamp.html?category_id=2">Test LED lamp</a>
    <a class="description">Warm white lamp</a>
  </div>
  <div class="quantity">Availability: On olemas 10 tk</div>
  <div class="price myyn">Price: 3.00 EUR</div>
</div></body></html>
"""


class ApiCatalogRetailerTests(SimpleTestCase):
    def test_stokker_product_uses_public_api_fields(self):
        row = catalog_api_retailers_parser.normalize_stokker_product(
            {
                "ItemID": "A27401&3M",
                "NameC": "Fiber disc",
                "ItemBarcode": "30051141550733",
                "PriceWithVat": 3.50,
                "CustomerPriceWithVat": 2.95,
                "CanBuy": True,
                "CategoryCode": "AB01-05",
                "CategoryName": "Fiber discs",
                "ImageL": "https://img.test/disc.jpg",
                "LinkToProducts": "https://www.stokker.ee/et/discs/A27401&3M/disc",
                "Description": "Long-life abrasive",
            }
        )

        self.assertEqual(row[0], "Fiber disc")
        self.assertEqual(row[1], 3.5)
        self.assertEqual(row[2], 2.95)
        self.assertEqual(row[4], "30051141550733")
        self.assertEqual(row[5], "stokker-A27401&3M")
        self.assertEqual(row[8], "A27401&3M")

    def test_esvika_product_requires_stock_and_uses_public_price(self):
        product = {
            "id": 14562,
            "productName": "Cable terminal",
            "url": "/electrical/cable-terminal",
            "supplierDescription": "Terminal 6 mm",
            "priceIncludingVAT": 0.05,
            "askPrice": False,
            "pictureId": 504,
            "availabilities": [{"inventoryAmountValue": 12}],
        }

        row = catalog_api_retailers_parser.normalize_esvika_product(
            product, "https://pood.esvika.ee/"
        )

        self.assertEqual(row[0], "Cable terminal")
        self.assertEqual(row[1], 0.05)
        self.assertEqual(row[5], "esvika-14562")
        self.assertEqual(row[6], "https://pood.esvika.ee/ProductPicture/504")
        product["availabilities"] = [{"inventoryAmountValue": 0}]
        self.assertIsNone(
            catalog_api_retailers_parser.normalize_esvika_product(
                product, "https://pood.esvika.ee/"
            )
        )

    def test_stokker_catalog_continues_past_page_100(self):
        requested_pages = []

        async def fake_request_json(*args, params, **kwargs):
            page = params["page"]
            requested_pages.append(page)
            return [{"ItemID": f"item-{page}"}] if page <= 100 else []

        with (
            patch.object(catalog_api_retailers_parser, "request_json", fake_request_json),
            patch.object(catalog_api_retailers_parser, "STOKKER_PAGE_SIZE", 1),
        ):
            products = asyncio.run(
                catalog_api_retailers_parser.fetch_stokker_products(
                    None,
                    catalog_api_retailers_parser.API_RETAILERS["stokker"],
                )
            )

        self.assertEqual(len(products), 100)
        self.assertIn(101, requested_pages)


class SitemapCatalogRetailerTests(SimpleTestCase):
    def test_torujyri_jsonld_product_is_normalized(self):
        row = catalog_sitemap_retailers_parser.parse_torujyri_page(
            TORUJYRI_HTML,
            "https://www.torujyri.ee/toode/alupex-pipe/",
        )

        self.assertEqual(row[0], "Alupex pipe 20 mm")
        self.assertEqual(row[1], 27.5)
        self.assertEqual(row[4], "4740000000001")
        self.assertEqual(row[8], "PIPE-20")
        self.assertEqual(row[9], "Alupex")

    def test_arcade_product_page_is_normalized(self):
        row = catalog_sitemap_retailers_parser.parse_arcade_page(
            ARCADE_HTML,
            "https://www.arcade.ee/drain-pipe-connector.html",
        )

        self.assertEqual(row[0], "Drain pipe connector")
        self.assertEqual(row[1], 4.2)
        self.assertEqual(row[5], "arcade-32")
        self.assertEqual(row[8], "32")
        self.assertEqual(row[9], "Pipes")

    def test_product_sitemap_index_keeps_all_locations(self):
        xml = """
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://shop.test/product-sitemap1.xml</loc></sitemap>
          <sitemap><loc>https://shop.test/product-sitemap2.xml</loc></sitemap>
        </sitemapindex>
        """
        self.assertEqual(
            catalog_sitemap_retailers_parser.parse_sitemap_locations(xml),
            [
                "https://shop.test/product-sitemap1.xml",
                "https://shop.test/product-sitemap2.xml",
            ],
        )


class ListingCatalogRetailerTests(SimpleTestCase):
    def test_hammerjack_navigation_paging_and_product_are_normalized(self):
        categories = catalog_listing_retailers_parser.discover_hammerjack_categories(
            HAMMERJACK_HTML,
            "https://hammerjack.eu/et",
        )
        rows = catalog_listing_retailers_parser.parse_hammerjack_page(
            HAMMERJACK_HTML,
            category_name="Tools",
            category_url="https://hammerjack.eu/et/tools",
            base_url="https://hammerjack.eu/et",
        )

        self.assertEqual(categories, [("Tools", "https://hammerjack.eu/et/tools")])
        self.assertEqual(catalog_listing_retailers_parser.hammerjack_page_count(HAMMERJACK_HTML), 3)
        self.assertEqual(rows[0][0], "Test drill")
        self.assertEqual(rows[0][1], 120.0)
        self.assertEqual(rows[0][2], 99.0)
        self.assertEqual(rows[0][5], "hammerjack-99")
        self.assertEqual(rows[0][8], "DRILL-99")

    def test_elektrikaup_complete_listing_is_normalized(self):
        rows = catalog_listing_retailers_parser.parse_elektrikaup_catalog(
            ELEKTRIKAUP_HTML,
            "https://www.elektrikaup.ee/",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Test LED lamp")
        self.assertEqual(rows[0][1], 3.0)
        self.assertEqual(rows[0][5], "elektrikaup-10783")
        self.assertEqual(rows[0][8], "10783")


class AdditionalRetailerAdapterTests(SimpleTestCase):
    adapter_cases = (
        (StokkerAdapter, "parsers.adapters.catalog_api_retailers.catalog_api_retailers_parser.main"),
        (EsvikaAdapter, "parsers.adapters.catalog_api_retailers.catalog_api_retailers_parser.main"),
        (
            HammerjackAdapter,
            "parsers.adapters.catalog_listing_retailers.catalog_listing_retailers_parser.main",
        ),
        (
            ElektrikaupAdapter,
            "parsers.adapters.catalog_listing_retailers.catalog_listing_retailers_parser.main",
        ),
        (
            ToruJyriAdapter,
            "parsers.adapters.catalog_sitemap_retailers.catalog_sitemap_retailers_parser.main",
        ),
        (
            ArcadeAdapter,
            "parsers.adapters.catalog_sitemap_retailers.catalog_sitemap_retailers_parser.main",
        ),
    )

    def test_adapters_create_valid_excel_and_count_products(self):
        async def fake_main(store_code, output_path, log_callback=None):
            save_excel(
                [[
                    f"{store_code} product",
                    10.0,
                    "",
                    "",
                    "",
                    f"{store_code}-1",
                    "https://img.test/product.jpg",
                    f"https://shop.test/{store_code}-product",
                    "SKU-1",
                    "Tools",
                    f"{store_code}-category-tools",
                    "Test product",
                ]],
                Path(output_path),
            )
            log_callback(f"{store_code} progress")

        for adapter_class, patch_target in self.adapter_cases:
            with self.subTest(adapter=adapter_class.code), tempfile.TemporaryDirectory() as tmp_dir:
                output_path = Path(tmp_dir) / f"{adapter_class.code}.xlsx"
                logs = []
                with patch(patch_target, fake_main):
                    adapter = adapter_class()
                    result = asyncio.run(adapter.run(output_path, log_callback=logs.append))

                validation = ExcelCatalogValidator().validate(
                    output_path,
                    column_map=adapter.column_map,
                    worksheet_name=adapter.worksheet_name,
                )
                self.assertTrue(result.success)
                self.assertEqual(result.products_count, 1)
                self.assertTrue(validation.is_valid)
                self.assertEqual(logs, [f"{adapter.code} progress"])

    def test_registry_contains_all_additional_retailers(self):
        expected = {
            "hammerjack": HammerjackAdapter,
            "stokker": StokkerAdapter,
            "torujyri": ToruJyriAdapter,
            "esvika": EsvikaAdapter,
            "arcade": ArcadeAdapter,
            "elektrikaup": ElektrikaupAdapter,
        }
        for code, adapter_class in expected.items():
            self.assertIs(ADAPTERS[code], adapter_class)
            self.assertIs(get_adapter_class(code), adapter_class)
