"""Exercise actual button JavaScript and the untrusted native message boundary."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
import lcsc_web_bridge as bridge

TOKEN = "document-1234"
PAGE = "https://item.szlcsc.com/2392.html"
PRODUCT = {"@type": "Product", "url": PAGE, "sku": "C2040"}


class MessageContractTests(unittest.TestCase):
    def message(self, **changes):
        payload = dict(version=1, type="import", id="C2040", url=PAGE, token=TOKEN)
        payload.update(changes)
        return json.dumps(payload)

    def test_accept_current_document_click(self):
        self.assertEqual(bridge.parse_import_message(self.message(), PAGE, TOKEN), "C2040")
        self.assertEqual(
            bridge.parse_import_message(
                self.message(url="https://item.szlcsc.com:443/2392.html"), PAGE, TOKEN
            ),
            "C2040",
        )

    def test_reject_old_documents_foreign_origins_and_schema_changes(self):
        cases = [
            self.message(token="old-document"),
            self.message(url="https://item.szlcsc.com/2403.html"),
            self.message(url=PAGE + "?another-product"),
            self.message(url="https://item.szlcsc.com.evil.test/2392.html"),
            self.message(url="https://user@item.szlcsc.com/2392.html"),
            self.message(url="https://item.szlcsc.com:8080/2392.html"),
            self.message(url="C2040"),
            self.message(url=None),
            self.message(id="C0"),
            self.message(id="C02040"),
            self.message(id="C2040 C2051"),
            self.message(id="c2040"),
            self.message(id="C２０４０"),
            self.message(id="C" + "1" * 40),
            self.message(id=None),
            self.message(type="execute"),
            self.message(version=True),
            self.message(version=2),
            self.message(path="D:/arbitrary"),
            "[]",
            "{}",
            "{broken",
            " " * 8193,
        ]
        for raw in cases:
            with self.subTest(raw=raw[:100]):
                with self.assertRaises(ValueError):
                    bridge.parse_import_message(raw, PAGE, TOKEN)

    def test_reject_matching_foreign_urls(self):
        foreign = "https://example.org/product-detail/C2040.html"
        with self.assertRaises(ValueError):
            bridge.parse_import_message(self.message(url=foreign), foreign, TOKEN)


_DOM_HARNESS = r"""
const vm = require('vm');
let sent = [];
let nodes = [];
let domWrites = 0;
let metadata = INITIAL_METADATA;
let rows = [];
global.window = {location: {href: INITIAL_URL}};
window.top = window;
window.lcscImport = {postMessage: raw => sent.push(JSON.parse(raw))};
global.document = {
    body: {appendChild: node => {node.isConnected = true; nodes.push(node);}},
    getElementById: id => nodes.find(node => node.isConnected && node.id === id) || null,
    querySelectorAll: selector => selector === 'script[type="application/ld+json"]' ?
        metadata.map(value => ({textContent: JSON.stringify(value)})) :
        selector === 'button[data-custom-data][data-spm="iad"]' ?
        rows.filter(row => row.isConnected).flatMap(row => row.carts.filter(cart =>
            cart.isConnected && cart.getAttribute('data-custom-data') !== null &&
            cart.getAttribute('data-spm') === 'iad')) : [],
    createElement: tag => new Proxy({
        tagName: tag,
        style: new Proxy({}, {set(target, name, value) {domWrites++; target[name] = value; return true;}}),
        isConnected: false, listeners: {}, attributes: {},
        setAttribute: function (name, value) {domWrites++; this.attributes[name] = value;},
        getAttribute: function (name) {return this.attributes[name] ?? null;},
        removeAttribute: function (name) {domWrites++; delete this.attributes[name];},
        addEventListener: function (name, fn) {this.listeners[name] = fn;},
        remove: function () {
            this.isConnected = false;
            if (this.parentElement) {
                const siblings = this.parentElement.children;
                siblings.splice(siblings.indexOf(this), 1);
                this.parentElement = null;
            }
        },
        click: function () {this.listeners.click({preventDefault(){}, stopPropagation(){}});}
    }, {set(target, name, value) {domWrites++; target[name] = value; return true;}})
};
// Search-row fixture follows the observed self-operated DOM: three independent
// identifiers (cart metadata, numbered dl, and the item's metadata-bearing link).
function addRow(id, options = {}) {
    const row = {
        isConnected: true,
        entries: options.entries ?? [{label: '编号', value: id}],
        links: options.links ?? [{id, href: 'https://item.szlcsc.com/168972.html?source=search'}],
        carts: [],
        querySelectorAll: function (selector) {
            if (selector === 'dl') return this.entries.map(entry => ({
                closest: () => row,
                querySelector: name => ({textContent: name === 'dt' ? entry.label : entry.value})
            }));
            if (selector === 'a[data-custom-data]') return this.links.map(link => ({
                closest: () => row, href: link.href,
                getAttribute: () => JSON.stringify({productType: 'electronic', productCode: link.id})
            }));
            return [];
        }
    };
    const cart = {
        isConnected: true, textContent: options.text ?? '加入购物车', disabled: false,
        attributes: {
            'data-spm': 'iad',
            'data-custom-data': JSON.stringify({productType: 'electronic', productCode: id}),
            ...(options.attributes || {})
        },
        parentElement: {children: []},
        getAttribute: function (name) {return this.attributes[name] ?? null;},
        setAttribute: function (name, value) {domWrites++; this.attributes[name] = value;},
        removeAttribute: function (name) {domWrites++; delete this.attributes[name];},
        closest: () => options.outside ? null : row,
        get nextElementSibling() {
            return this.parentElement.children[this.parentElement.children.indexOf(this) + 1] || null;
        },
        insertAdjacentElement: function (position, node) {
            if (position !== 'afterend') throw Error('Unexpected placement');
            domWrites++;
            if (node.parentElement) node.remove();
            const siblings = this.parentElement.children;
            siblings.splice(siblings.indexOf(this) + 1, 0, node);
            node.parentElement = this.parentElement;
            node.isConnected = true;
            if (!nodes.includes(node)) nodes.push(node);
        }
    };
    cart.parentElement.children.push(cart);
    row.carts.push(cart);
    rows.push(row);
    return row;
}
function removeRow(row) {
    row.isConnected = false;
    row.carts.forEach(cart => {
        cart.isConnected = false;
        cart.parentElement.children.forEach(node => {node.isConnected = false;});
    });
}
const rowButtons = () => nodes.filter(node => node.isConnected &&
    node.getAttribute('data-lcsc-kicad-import') === 'row');
const buttonFor = id => rowButtons().find(node => node.getAttribute('data-lcsc-component') === id);
const rowState = () => ({
    buttons: rowButtons().map(node => ({
        id: node.getAttribute('data-lcsc-component'), text: node.textContent,
        disabled: node.disabled, busy: node.getAttribute('aria-busy'),
        state: node.getAttribute('data-state'), title: node.title
    })),
    styles: nodes.filter(node => node.isConnected && node.tagName === 'style').length,
    writes: domWrites, sent: JSON.parse(JSON.stringify(sent)),
    runtime: Object.hasOwn(window, '__lcscKiCadImport')
});
const inject = () => vm.runInThisContext(INJECTION_SCRIPT);
const read = () => vm.runInThisContext(PRODUCT_SCRIPT);
const refresh = () => JSON.parse(vm.runInThisContext(REFRESH_SCRIPT));
const button = () => document.getElementById('lcsc-kicad-import');
const state = () => ({
    count: nodes.filter(node => node.isConnected && node.tagName === 'button').length,
    styles: nodes.filter(node => node.isConnected && node.tagName === 'style').length,
    hidden: button()?.hidden, disabled: button()?.disabled,
    text: button()?.textContent, title: button()?.title,
    attributes: {...button()?.attributes}, tag: button()?.tagName, type: button()?.type,
    display: button()?.style.display, sent: JSON.parse(JSON.stringify(sent)),
    writes: domWrites, runtime: Object.hasOwn(window, '__lcscKiCadImport')
});
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the WebView script checks")
class InjectedButtonTests(unittest.TestCase):
    def run_js(self, body, *, url=PAGE, products=None):
        script = (
            _DOM_HARNESS.replace(
                "INITIAL_METADATA", json.dumps(products if products is not None else [PRODUCT])
            )
            .replace("INITIAL_URL", json.dumps(url))
            .replace("INJECTION_SCRIPT", json.dumps(bridge.injection_script(TOKEN)))
            .replace("PRODUCT_SCRIPT", json.dumps(bridge.current_product_script()))
            .replace("REFRESH_SCRIPT", json.dumps(bridge.refresh_script(TOKEN)))
            + body
        )
        process = subprocess.run(["node", "-"], input=script, capture_output=True, encoding="utf-8")
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def test_reinjection_never_imports_and_single_click_blocks_double_submit(self):
        result = self.run_js(
            "inject();inject();const before=state();button().click();button().click();"
            "inject();process.stdout.write(JSON.stringify({before,after:state()}));"
        )
        self.assertEqual(result["before"]["count"], 1)
        self.assertEqual(result["before"]["styles"], 1)
        self.assertEqual(result["before"]["sent"], [])
        self.assertEqual(result["before"]["text"], "导入 KiCad")
        self.assertEqual(result["before"]["attributes"]["data-state"], "idle")
        self.assertEqual(result["before"]["tag"], "button")
        self.assertEqual(result["before"]["type"], "button")
        self.assertEqual(result["after"]["count"], 1)
        self.assertEqual(result["after"]["styles"], 1)
        self.assertTrue(result["after"]["disabled"])
        self.assertEqual(result["after"]["attributes"]["data-state"], "importing")
        self.assertEqual(result["after"]["attributes"]["aria-busy"], "true")
        self.assertIn("导入中", result["after"]["attributes"]["aria-label"])
        self.assertEqual(
            result["after"]["sent"],
            [dict(version=1, type="import", id="C2040", url=PAGE, token=TOKEN)],
        )

    def test_only_primary_product_not_recommendations(self):
        products = [PRODUCT, dict(PRODUCT, url="https://item.szlcsc.com/2403.html", sku="C2051")]
        result = self.run_js(
            "inject();button().click();process.stdout.write(JSON.stringify(state()));",
            products=products,
        )
        self.assertEqual(result["sent"][0]["id"], "C2040")

    def test_unknown_ambiguous_search_and_verification_pages_are_untouched(self):
        for url, products in (
            (PAGE, []),
            (PAGE, [PRODUCT, dict(PRODUCT, sku="C2051")]),
            (PAGE, [dict(PRODUCT, url="https://item.szlcsc.com/2403.html")]),
            ("https://so.szlcsc.com/global.html?k=C2040", [PRODUCT]),
            ("https://item.szlcsc.com.evil.test/2392.html", [PRODUCT]),
            ("https://www.lcsc.com/product-detail/C2040.html", []),
            ("https://www.szlcsc.com/security-check", []),
        ):
            with self.subTest(url=url, products=products):
                result = self.run_js(
                    "inject();inject();refresh();process.stdout.write(JSON.stringify(state()));",
                    url=url,
                    products=products,
                )
                self.assertEqual(result["count"], 0)
                self.assertEqual(result["styles"], 0)
                self.assertEqual(result["writes"], 0)
                self.assertFalse(result["runtime"])
                self.assertEqual(result["sent"], [])

    def test_explicit_product_url_and_conflicting_metadata(self):
        url = "https://www.lcsc.com/product-detail/MCU_C2040.html?recommend=C2051"
        result = self.run_js("process.stdout.write(JSON.stringify(read()));", url=url, products=[])
        self.assertEqual(result, "")
        result = self.run_js(
            "process.stdout.write(JSON.stringify(read()));",
            url=url,
            products=[dict(PRODUCT, url=url)],
        )
        self.assertEqual(result, "C2040")
        result = self.run_js(
            "process.stdout.write(JSON.stringify(read()));",
            url=url,
            products=[dict(PRODUCT, url=url, sku="C2051")],
        )
        self.assertEqual(result, "")

    def test_route_change_before_click_reads_new_primary_product(self):
        result = self.run_js(
            "inject();window.location.href='https://item.szlcsc.com/2403.html';"
            "metadata=[{'@type':'Product',url:window.location.href,sku:'C2051'}];"
            "button().click();process.stdout.write(JSON.stringify(state()));"
        )
        self.assertEqual(result["sent"][0]["id"], "C2051")
        self.assertEqual(result["sent"][0]["url"], "https://item.szlcsc.com/2403.html")

    def test_repeated_poll_and_injection_reuse_runtime_without_dom_writes(self):
        result = self.run_js(
            "inject();const runtime=window.__lcscKiCadImport;"
            "const functions=[runtime.readProduct,runtime.refresh,runtime.paint,runtime.status];"
            "const before=state();inject();const probe=refresh();inject();"
            "process.stdout.write(JSON.stringify({before,after:state(),probe,"
            "sameRuntime:runtime===window.__lcscKiCadImport,"
            "sameFunctions:functions.every((fn,i)=>fn===[runtime.readProduct,runtime.refresh,"
            "runtime.paint,runtime.status][i])}));"
        )
        self.assertTrue(result["sameRuntime"])
        self.assertTrue(result["sameFunctions"])
        self.assertEqual(result["before"]["writes"], result["after"]["writes"])
        self.assertEqual(result["probe"], {"installed": True, "componentId": "C2040"})

    def test_probe_only_reads_until_product_metadata_is_available(self):
        result = self.run_js(
            "const empty=refresh();metadata=[{'@type':'Product',url:window.location.href,"
            "sku:'C2040'}];const confirmed=refresh();const before=state();"
            "inject();process.stdout.write(JSON.stringify({empty,confirmed,before,after:state()}));",
            products=[],
        )
        self.assertEqual(result["empty"], {"installed": False, "componentId": ""})
        self.assertEqual(result["confirmed"], {"installed": False, "componentId": "C2040"})
        self.assertEqual(result["before"]["writes"], 0)
        self.assertFalse(result["before"]["runtime"])
        self.assertEqual(result["after"]["count"], 1)

    def test_verification_transition_removes_own_dom_and_runtime(self):
        result = self.run_js(
            "inject();const previous=button();metadata=[];const probe=refresh();"
            "const removed=state();previous.click();inject();refresh();"
            "process.stdout.write(JSON.stringify({probe,removed,after:state()}));"
        )
        self.assertEqual(result["probe"], {"installed": False, "componentId": ""})
        self.assertEqual(result["removed"]["count"], 0)
        self.assertEqual(result["removed"]["styles"], 0)
        self.assertFalse(result["removed"]["runtime"])
        self.assertEqual(result["removed"]["writes"], result["after"]["writes"])
        self.assertEqual(result["after"]["sent"], [])

    def test_probe_disposes_old_token_then_requests_reinstallation(self):
        fresh = bridge.refresh_script("fresh-document")
        result = self.run_js(
            "inject();const probe=JSON.parse(vm.runInThisContext("
            + json.dumps(fresh)
            + "));process.stdout.write(JSON.stringify({probe,state:state()}));"
        )
        self.assertEqual(result["probe"], {"installed": False, "componentId": "C2040"})
        self.assertEqual(result["state"]["count"], 0)
        self.assertEqual(result["state"]["styles"], 0)
        self.assertFalse(result["state"]["runtime"])

    def test_repeated_status_is_not_repainted(self):
        status = json.dumps(bridge.status_script("C2040", "importing", "正在导入"))
        result = self.run_js(
            "inject();vm.runInThisContext(" + status + ");const before=state();"
            "vm.runInThisContext(" + status + ");refresh();"
            "process.stdout.write(JSON.stringify({before,after:state()}));"
        )
        self.assertEqual(result["before"]["writes"], result["after"]["writes"])
        self.assertEqual(result["after"]["attributes"]["data-state"], "importing")

    def test_status_on_verification_page_removes_button_without_new_nodes(self):
        status = json.dumps(bridge.status_script("C2040", "success", "已导入"))
        result = self.run_js(
            "inject();button().click();metadata=[];vm.runInThisContext(" + status + ");"
            "process.stdout.write(JSON.stringify(state()));"
        )
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["styles"], 0)
        self.assertFalse(result["runtime"])

    def test_subframes_are_never_modified(self):
        result = self.run_js(
            "window.top={};inject();refresh();process.stdout.write(JSON.stringify(state()));"
        )
        self.assertEqual(result["writes"], 0)
        self.assertFalse(result["runtime"])

    def test_removed_button_is_reinserted_and_new_token_replaces_old_listener(self):
        fresh = bridge.injection_script("fresh-document")
        result = self.run_js(
            "inject();button().remove();inject();"
            + "vm.runInThisContext("
            + json.dumps(fresh)
            + ");button().click();"
            + "process.stdout.write(JSON.stringify(state()));"
        )
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["styles"], 1)
        self.assertEqual(result["sent"][0]["token"], "fresh-document")

    def test_removed_styles_are_restored_without_another_button_or_import(self):
        result = self.run_js(
            "inject();document.getElementById('lcsc-kicad-import-style').remove();inject();"
            "process.stdout.write(JSON.stringify(state()));"
        )
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["styles"], 1)
        self.assertEqual(result["sent"], [])

    def test_probe_restores_removed_controls_during_import_without_resubmitting(self):
        result = self.run_js(
            "inject();button().click();button().remove();"
            "document.getElementById('lcsc-kicad-import-style').remove();refresh();"
            "button().click();process.stdout.write(JSON.stringify(state()));"
        )
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["styles"], 1)
        self.assertTrue(result["disabled"])
        self.assertEqual(result["attributes"]["data-state"], "importing")
        self.assertEqual(len(result["sent"]), 1)

    def test_completed_import_is_clickable_and_messages_are_literal(self):
        status = bridge.status_script(
            "C2040", "success", "导入成功 '</script>'; throw Error('unsafe')"
        )
        result = self.run_js(
            "inject();button().click();vm.runInThisContext(" + json.dumps(status) + ");"
            "const completed=state();button().click();"
            "process.stdout.write(JSON.stringify({completed,after:state()}));"
        )
        self.assertFalse(result["completed"]["disabled"])
        self.assertEqual(result["completed"]["text"], "已导入")
        self.assertEqual(result["completed"]["attributes"]["data-state"], "success")
        self.assertEqual(result["completed"]["attributes"]["aria-busy"], "false")
        self.assertIn("throw Error('unsafe')", result["completed"]["title"])
        self.assertEqual(len(result["after"]["sent"]), 2)

    def test_old_import_finishing_on_new_product_releases_busy_without_wrong_label(self):
        status = bridge.status_script("C2040", "success", "C2040 已导入")
        result = self.run_js(
            "inject();button().click();window.location.href='https://item.szlcsc.com/2403.html';"
            "metadata=[{'@type':'Product',url:window.location.href,sku:'C2051'}];inject();"
            "const during=state();vm.runInThisContext(" + json.dumps(status) + ");"
            "process.stdout.write(JSON.stringify({during,after:state()}));"
        )
        self.assertTrue(result["during"]["disabled"])
        self.assertFalse(result["after"]["disabled"])
        self.assertEqual(result["after"]["text"], "导入 KiCad")
        self.assertEqual(result["after"]["attributes"]["data-state"], "idle")
        self.assertNotIn("C2040", result["after"]["title"])

    def test_missing_bridge_and_send_failure_allow_retry(self):
        for broken in (
            "delete window.lcscImport;",
            "window.lcscImport.postMessage=()=>{throw Error('offline')};",
        ):
            result = self.run_js(
                "inject();"
                + broken
                + "button().click();process.stdout.write(JSON.stringify(state()));"
            )
            self.assertFalse(result["disabled"])
            self.assertEqual(result["text"], "重试")
            self.assertEqual(result["attributes"]["data-state"], "error")
            self.assertEqual(result["attributes"]["aria-live"], "polite")
            self.assertEqual(result["sent"], [])


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the WebView script checks")
class SearchRowButtonTests(unittest.TestCase):
    def run_js(self, body, *, url="https://so.szlcsc.com/global.html?k=C2040"):
        return InjectedButtonTests.run_js(self, body, url=url, products=[])

    def selected(self, component_id):
        return (
            "vm.runInThisContext(" + json.dumps(bridge.current_product_script(component_id)) + ")"
        )

    def status(self, component_id, state, message=""):
        return (
            "vm.runInThisContext("
            + json.dumps(bridge.status_script(component_id, state, message))
            + ");"
        )

    def test_second_row_click_selects_its_id_and_locks_all_buttons(self):
        result = self.run_js(
            "addRow('C2040');addRow('C2051');inject();const before=rowState();"
            "const selected=" + self.selected("C2051") + ";"
            "buttonFor('C2051').click();buttonFor('C2040').click();buttonFor('C2051').click();"
            "process.stdout.write(JSON.stringify({before,selected,main:read(),after:rowState()}));"
        )
        self.assertEqual([item["id"] for item in result["before"]["buttons"]], ["C2040", "C2051"])
        self.assertEqual(result["before"]["sent"], [])
        self.assertEqual(result["selected"], "C2051")
        self.assertEqual(result["main"], "")
        self.assertEqual([item["disabled"] for item in result["after"]["buttons"]], [True, True])
        self.assertEqual(
            [item["state"] for item in result["after"]["buttons"]], ["idle", "importing"]
        )
        self.assertEqual([item["busy"] for item in result["after"]["buttons"]], ["false", "true"])
        self.assertEqual([item["text"] for item in result["after"]["buttons"]], ["导入", "导入中"])
        self.assertEqual(len(result["after"]["sent"]), 1)
        self.assertEqual(result["after"]["sent"][0]["id"], "C2051")
        self.assertEqual(
            result["after"]["sent"][0]["url"], "https://so.szlcsc.com/global.html?k=C2040"
        )

    def test_selected_identity_is_strict_on_detail_pages_too(self):
        result = InjectedButtonTests.run_js(
            self,
            "process.stdout.write(JSON.stringify(["
            + self.selected("C2040")
            + ","
            + self.selected("C2051")
            + "]));",
        )
        self.assertEqual(result, ["C2040", ""])
        for value in ("C0", "c2040", "C2040 C2051", "C" + "1" * 32, {}, 2040):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bridge.current_product_script(value)

    def test_conflicting_missing_and_marketplace_metadata_are_not_importable(self):
        invalid = [
            {"entries": [{"label": "编号", "value": "C2051"}]},
            {"entries": [{"label": "供应商编号", "value": "C2040"}]},
            {"entries": [{"label": "编号", "value": "168972"}]},
            {"entries": [{"label": "编号", "value": "C2040"}, {"label": "编号", "value": "C2051"}]},
            {"links": []},
            {"links": [{"id": "C2051", "href": "https://item.szlcsc.com/168972.html"}]},
            {"links": [{"id": "C2040", "href": "https://item.szlcsc.com.evil.test/168972.html"}]},
            {"links": [{"id": "C2040", "href": "https://user@item.szlcsc.com/168972.html"}]},
            {"links": [{"id": "C2040", "href": "https://item.szlcsc.com:8080/168972.html"}]},
            {"attributes": {"data-custom-data": None}},
            {"attributes": {"data-custom-data": "{broken"}},
            {
                "attributes": {
                    "data-custom-data": json.dumps(
                        {"productType": "marketplace", "productCode": "C2040"}
                    )
                }
            },
            {
                "attributes": {
                    "data-custom-data": json.dumps(
                        {"productType": "electronic", "productCode": "C2051"}
                    )
                }
            },
            {"outside": True},
        ]
        for options in invalid:
            with self.subTest(options=options):
                result = self.run_js(
                    "addRow('C2040'," + json.dumps(options) + ");inject();"
                    "const selected=" + self.selected("C2040") + ";"
                    "process.stdout.write(JSON.stringify({selected,state:rowState()}));"
                )
                self.assertEqual(result["selected"], "")
                self.assertEqual(result["state"]["buttons"], [])
                self.assertEqual(result["state"]["writes"], 0)
                self.assertFalse(result["state"]["runtime"])

    def test_unrelated_query_and_internal_product_number_never_authorize(self):
        result = self.run_js(
            "addRow('C2040');inject();process.stdout.write(JSON.stringify(["
            + self.selected("C2051")
            + ","
            + self.selected("C168972")
            + ","
            + self.selected("C2040")
            + "]));",
            url="https://so.szlcsc.com/global.html?k=C2051",
        )
        self.assertEqual(result, ["", "", "C2040"])

    def test_verification_foreign_and_subframe_documents_stay_untouched(self):
        for url in (
            "https://so.szlcsc.com/security-check",
            "https://so.szlcsc.com.evil.test/global.html?k=C2040",
            "https://user@so.szlcsc.com/global.html?k=C2040",
            "https://so.szlcsc.com:8080/global.html?k=C2040",
            "https://item.szlcsc.com/168972.html",
        ):
            with self.subTest(url=url):
                result = self.run_js(
                    "addRow('C2040');inject();refresh();process.stdout.write(JSON.stringify(rowState()));",
                    url=url,
                )
                self.assertEqual(result["writes"], 0)
                self.assertFalse(result["runtime"])
        result = self.run_js(
            "addRow('C2040');window.top={};inject();refresh();process.stdout.write(JSON.stringify(rowState()));"
        )
        self.assertEqual(result["writes"], 0)

    def test_recycled_cart_does_not_retarget_a_stale_button_click(self):
        result = self.run_js(
            "const row=addRow('C2040');inject();const old=buttonFor('C2040');"
            "row.entries[0].value='C2051';row.links[0].id='C2051';"
            "row.carts[0].setAttribute('data-custom-data',JSON.stringify({productType:'electronic',productCode:'C2051'}));"
            "old.click();const afterOld=rowState();buttonFor('C2051').click();"
            "process.stdout.write(JSON.stringify({afterOld,afterNew:rowState()}));"
        )
        self.assertEqual(result["afterOld"]["sent"], [])
        self.assertEqual([item["id"] for item in result["afterOld"]["buttons"]], ["C2051"])
        self.assertEqual(result["afterNew"]["sent"][0]["id"], "C2051")

    def test_removed_row_button_cannot_submit_a_different_current_row(self):
        result = self.run_js(
            "const first=addRow('C2040');addRow('C2051');inject();const old=buttonFor('C2040');"
            "removeRow(first);old.click();process.stdout.write(JSON.stringify(rowState()));"
        )
        self.assertEqual(result["sent"], [])
        self.assertEqual([item["id"] for item in result["buttons"]], ["C2051"])

    def test_replaced_row_and_removed_control_restore_busy_state_without_resubmit(self):
        result = self.run_js(
            "const first=addRow('C2040');addRow('C2051');inject();buttonFor('C2040').click();"
            "removeRow(first);addRow('C2040');refresh();buttonFor('C2040').remove();"
            "document.getElementById('lcsc-kicad-import-style').remove();refresh();"
            "buttonFor('C2040').click();const busy=rowState();"
            + self.status("C2040", "success", "已导入")
            + "process.stdout.write(JSON.stringify({busy,done:rowState()}));"
        )
        self.assertEqual(len(result["busy"]["sent"]), 1)
        self.assertEqual(result["busy"]["styles"], 1)
        active = next(item for item in result["busy"]["buttons"] if item["id"] == "C2040")
        self.assertEqual(active["state"], "importing")
        self.assertTrue(all(item["disabled"] for item in result["busy"]["buttons"]))
        self.assertTrue(all(not item["disabled"] for item in result["done"]["buttons"]))
        states = {item["id"]: item["state"] for item in result["done"]["buttons"]}
        self.assertEqual(states, {"C2040": "success", "C2051": "idle"})

    def test_success_and_error_feedback_belongs_only_to_the_matching_product(self):
        result = self.run_js(
            "addRow('C2040');addRow('C2051');inject();buttonFor('C2040').click();"
            + self.status("C2040", "success", "已导入")
            + "buttonFor('C2051').click();"
            + self.status("C2051", "error", "下载失败")
            + "const failed=rowState();buttonFor('C2051').click();"
            + "process.stdout.write(JSON.stringify({failed,retry:rowState()}));"
        )
        self.assertEqual([item["text"] for item in result["failed"]["buttons"]], ["已导入", "重试"])
        self.assertNotIn("下载失败", result["failed"]["buttons"][0]["title"])
        self.assertIn("下载失败", result["failed"]["buttons"][1]["title"])
        self.assertEqual(
            [item["id"] for item in result["retry"]["sent"]], ["C2040", "C2051", "C2051"]
        )

    def test_new_page_never_shows_previous_product_success(self):
        result = self.run_js(
            "const first=addRow('C2040');inject();buttonFor('C2040').click();removeRow(first);"
            "window.location.href='https://so.szlcsc.com/global.html?page=2';addRow('C2051');refresh();"
            + self.status("C2040", "success", "C2040 已导入")
            + "process.stdout.write(JSON.stringify(rowState()));"
        )
        self.assertEqual(result["buttons"][0]["id"], "C2051")
        self.assertEqual(result["buttons"][0]["state"], "idle")
        self.assertFalse(result["buttons"][0]["disabled"])
        self.assertNotIn("C2040", result["buttons"][0]["title"])

    def test_read_probe_and_repeated_refresh_do_not_write_or_import(self):
        result = self.run_js(
            "const row=addRow('C2040');const before=refresh();const probe=rowState();inject();"
            "const installed=rowState();inject();refresh();const repeated=rowState();"
            "process.stdout.write(JSON.stringify({before,probe,installed,repeated,"
            "adjacent:row.carts[0].nextElementSibling===buttonFor('C2040')}));"
        )
        self.assertEqual(result["before"], {"installed": False, "componentId": "C2040"})
        self.assertEqual(result["probe"]["writes"], 0)
        self.assertEqual(result["installed"]["writes"], result["repeated"]["writes"])
        self.assertEqual(result["repeated"]["sent"], [])
        self.assertTrue(result["adjacent"])

    def test_dispose_restores_only_own_cart_marker_and_does_not_move_cart(self):
        result = self.run_js(
            "const row=addRow('C2040');const cart=row.carts[0];const parent=cart.parentElement;"
            "const original=JSON.stringify(cart.attributes);inject();const old=buttonFor('C2040');"
            "row.entries=[];const probe=refresh();const removed=rowState();old.click();refresh();"
            "process.stdout.write(JSON.stringify({probe,removed,after:rowState(),"
            "sameParent:cart.parentElement===parent,attrsRestored:JSON.stringify(cart.attributes)===original,"
            "cartText:cart.textContent,cartDisabled:cart.disabled}));"
        )
        self.assertEqual(result["probe"], {"installed": False, "componentId": ""})
        self.assertEqual(result["removed"]["buttons"], [])
        self.assertEqual(result["removed"]["styles"], 0)
        self.assertEqual(result["removed"]["writes"], result["after"]["writes"])
        self.assertTrue(result["sameParent"])
        self.assertTrue(result["attrsRestored"])
        self.assertEqual(result["cartText"], "加入购物车")
        self.assertFalse(result["cartDisabled"])


if __name__ == "__main__":
    unittest.main()
