"""URL and main-product detection checks without loading KiCad's plugin package."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "plugins" / "lcsc_page.py"
SPEC = importlib.util.spec_from_file_location("lcsc_page", MODULE_PATH)
assert SPEC and SPEC.loader
lcsc_page = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lcsc_page)


class ComponentIdTests(unittest.TestCase):
    def test_standalone_ids(self):
        for text, expected in (("C2040", "C2040"), ("  c2040 \n", "C2040"), ("C1", "C1")):
            with self.subTest(text=text):
                self.assertEqual(lcsc_page.normalize_component_id(text), expected)

    def test_only_ascii_complete_ids(self):
        for text in (
            "2040",
            "C",
            "C0",
            "C02040",
            "part C2040",
            "C2040 C2051",
            "C20.40",
            "C２０４０",
            "C2040.html",
            "C20\n40",
        ):
            with self.subTest(text=text):
                self.assertIsNone(lcsc_page.normalize_component_id(text))


class ProductUrlTests(unittest.TestCase):
    def test_explicit_product_paths(self):
        for url in (
            "https://www.lcsc.com/product-detail/C2040.html",
            "https://lcsc.com/product-detail/Microcontrollers_Raspberry-Pi-RP2040_C2040.html",
            "https://www.lcsc.com/product-detail/Microcontrollers/RP2040_C2040.html",
            "https://www.lcsc.com/product-detail/%E6%A0%91%E8%8E%93%E6%B4%BE_C2040.html",
            "https://www.lcsc.com/product-detail/c2040.html?recommended=C2051#C25804",
            "https://www.lcsc.cn/item/C2040.html",
            "https://www.szlcsc.com/item/C2040.html",
            "http://www.szlcsc.com/product-detail/C2040.html",
        ):
            with self.subTest(url=url):
                self.assertEqual(lcsc_page.component_id_from_url(url), "C2040")

    def test_do_not_infer_from_search_recommendations_or_internal_ids(self):
        for url in (
            "https://item.szlcsc.com/2392.html",
            "https://www.lcsc.cn/item/2392.html",
            "https://www.lcsc.com/search?q=C2040",
            "https://www.lcsc.com/?productCode=C2040",
            "https://www.lcsc.com/product-detail/2392.html?sku=C2040",
            "https://www.lcsc.com/product-detail/notC2040.html",
            "https://www.lcsc.com/product-detail/C2040.html/more",
            "https://www.lcsc.com/recommendations/C2040.html",
            "https://www.lcsc.com/product-detail/C２０４０.html",
            "https://www.lcsc.com/product-detail/C0.html",
            "https://www.lcsc.com/product-detail/C02040.html",
            "https://www.lcsc.cn/item/C0.html",
            "https://www.lcsc.com/item/C2040.html",
        ):
            with self.subTest(url=url):
                self.assertIsNone(lcsc_page.component_id_from_url(url))

    def test_reject_spoofs_and_invalid_encoding(self):
        for url in (
            "https://lcsc.com.attacker.example/product-detail/C2040.html",
            "https://notlcsc.com/product-detail/C2040.html",
            "https://lcsc.com@attacker.example/product-detail/C2040.html",
            "https://attacker.example@lcsc.com/product-detail/C2040.html",
            "https://www.lcsc.com:bad/product-detail/C2040.html",
            "https://www.lcsc.com:99999/product-detail/C2040.html",
            "https://www.lcsc.com:8080/product-detail/C2040.html",
            "https://www.lcsc.com/product-detail/C2040.html?bad=%zz",
            "https://www.lcsc.com/product-detail/C2040.html?bad=%ff",
            "https://www.lcsc.com/product-detail/%ff_C2040.html",
            "https://www.lcsc.com/product-detail%2fC2040.html",
            "https://www.lcsc.com/product-detail/C2040.html%00",
            "https://www.lcsc.com/product-detail/C20\n40.html",
            "https://www.lcsc.com\\@attacker.example/product-detail/C2040.html",
            "javascript:https://www.lcsc.com/product-detail/C2040.html",
            "file:///product-detail/C2040.html",
        ):
            with self.subTest(url=url):
                self.assertIsNone(lcsc_page.component_id_from_url(url))


class NavigationTests(unittest.TestCase):
    def test_navigation(self):
        self.assertEqual(
            lcsc_page.normalize_navigation(" c2040 "),
            "https://so.szlcsc.com/global.html?k=C2040",
        )
        self.assertEqual(
            lcsc_page.normalize_navigation("WWW.SZLCSC.COM"), "https://www.szlcsc.com/"
        )
        self.assertEqual(
            lcsc_page.normalize_navigation("https://so.szlcsc.com/global.html?k=RP%202040"),
            "https://so.szlcsc.com/global.html?k=RP%202040",
        )
        self.assertEqual(lcsc_page.normalize_navigation("http://lcsc.cn"), "http://lcsc.cn/")

    def test_invalid_navigation(self):
        for text in (
            "",
            "2040",
            "C0",
            "C02040",
            "https://example.com",
            "https://evil-lcsc.com",
            "ftp://lcsc.com/",
            "javascript:alert(1)",
            "https://user:pass@lcsc.com/",
            "https://www..lcsc.com/",
            "https://-bad.lcsc.com/",
            "https://www.lcsc.com\\evil",
            "https://www.lcsc.com/%",
            "https://www.lcsc.com/?q=%0a",
        ):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    lcsc_page.normalize_navigation(text)


@unittest.skipUnless(shutil.which("node"), "Node.js is needed to execute the WebView expression")
class MainProductScriptTests(unittest.TestCase):
    def evaluate(self, url, documents):
        # A tiny DOM fixture: the script can only read JSON-LD elements, exactly
        # as in WebView. Real JSON-LD structure is taken from item/2392 (C2040).
        harness = (
            "global.window={location:{href:" + json.dumps(url) + "}};"
            "global.document={querySelectorAll:()=>"
            + json.dumps(
                [
                    {"textContent": value if isinstance(value, str) else json.dumps(value)}
                    for value in documents
                ]
            )
            + "};process.stdout.write(JSON.stringify("
            + lcsc_page.PRODUCT_ID_SCRIPT
            + "));"
        )
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_chinese_primary_product_and_recommendation(self):
        page = "https://item.szlcsc.com/2392.html"
        graph = {
            "@graph": [
                {"@type": "WebSite", "sku": "C999"},
                {"@type": "Product", "url": page, "sku": "C2040", "description": "推荐 C20625731"},
                {"@type": "Product", "url": "https://item.szlcsc.com/2403.html", "sku": "C2051"},
            ]
        }
        self.assertEqual(self.evaluate(page + "?from=search", [graph]), "C2040")

    def test_product_id_fragment(self):
        page = "https://item.szlcsc.com/2392.html"
        self.assertEqual(
            self.evaluate(
                page, [{"@type": ["Thing", "Product"], "@id": page + "#product", "sku": "c2040"}]
            ),
            "C2040",
        )

    def test_ambiguous_or_unmatched_metadata_is_not_imported(self):
        page = "https://item.szlcsc.com/2392.html"
        product = {"@type": "Product", "url": page, "sku": "C2040"}
        other = dict(product, sku="C2051")
        self.assertEqual(self.evaluate(page, [product, other]), "")
        self.assertEqual(
            self.evaluate(page, [dict(product, url="https://item.szlcsc.com/2403.html")]), ""
        )
        self.assertEqual(self.evaluate(page, [dict(product, sku="C0")]), "")
        self.assertEqual(self.evaluate(page, [dict(product, sku="C02040")]), "")
        self.assertEqual(
            self.evaluate(page, [{"@type": "Product", "sku": "C2040"}, "broken JSON"]), ""
        )
        self.assertEqual(self.evaluate("https://so.szlcsc.com/global.html?k=C2040", [product]), "")
        self.assertEqual(
            self.evaluate("https://item.szlcsc.com.evil.example/2392.html", [product]), ""
        )


if __name__ == "__main__":
    unittest.main()
