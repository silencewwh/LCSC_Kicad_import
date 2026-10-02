"""Opt-in native WebView smoke test for the search-result import button.

Run with KiCad's Python: python tools/smoke_lcsc_list.py [--hold]
Loads the real LCSC search page and clicks only its injected import button.
The native message and product-validation path are unchanged; _start_import
records the validated component and returns a mock success without downloading
assets, changing an existing project, or clicking the store's cart button.
With --hold, the passing page remains open until the window is closed.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

import wx  # noqa: E402
import wx.html2 as html2  # noqa: E402
from lcsc_browser import LCSCBrowser  # noqa: E402
from lcsc_web_bridge import current_product_script  # noqa: E402

COMPONENT = "C157624"
SEARCH_URL = f"https://so.szlcsc.com/global.html?k={COMPONENT}"

# Keep all queries scoped to the self-operated result, not recommendation cards.
_FIND_CONTROLS = r"""
var section = document.querySelector('section[data-spm="zy"]');
if (!section) return null;
var cart = Array.from(section.querySelectorAll('button[data-custom-data]')).find(function(node) {
    try {
        var data = JSON.parse(node.getAttribute('data-custom-data'));
        return data.productType === 'electronic' && data.productCode === 'C157624' &&
            node.textContent.trim() === '加入购物车';
    } catch (error) { return false; }
});
if (!cart) return null;
var button = cart.nextElementSibling;
if (!button || !button.matches('button[data-lcsc-kicad-import="row"]') ||
        button.getAttribute('data-lcsc-component') !== 'C157624') return null;
"""

BUTTON_STATE = (
    "(function(){"
    + _FIND_CONTROLS
    + r"""
function rect(node) {
    var r = node.getBoundingClientRect();
    return {left:r.left, right:r.right, top:r.top, bottom:r.bottom,
        width:r.width, height:r.height};
}
return JSON.stringify({
    text:button.textContent.trim(), title:button.title,
    disabled:button.disabled, hidden:button.hidden,
    state:button.getAttribute('data-state'), rect:rect(button),
    cart:{text:cart.textContent.trim(), disabled:cart.disabled,
        metadata:cart.getAttribute('data-custom-data'), rect:rect(cart)},
    sameParent:button.parentElement === cart.parentElement,
    parent:rect(cart.parentElement), viewport:innerWidth,
    cartClicks:window.__lcscListSmoke ? window.__lcscListSmoke.cartClicks : null
});
})()
"""
)

ARM_CART_OBSERVER = (
    "(function(){"
    + _FIND_CONTROLS
    + r"""
if (!window.__lcscListSmoke) {
    window.__lcscListSmoke = {cartClicks:0};
    document.addEventListener('click', function(event) {
        var target = event.target instanceof Element ? event.target.closest('button') : null;
        if (!target || !target.closest('section[data-spm="zy"]')) return;
        try {
            var data = JSON.parse(target.getAttribute('data-custom-data'));
            if (!data || data.productType !== 'electronic' || data.productCode !== 'C157624') return;
        } catch (error) { return; }
        window.__lcscListSmoke.cartClicks += 1;
        // A regression must fail the test without purchasing or adding anything.
        event.preventDefault();
        event.stopImmediatePropagation();
    }, true);
}
button.scrollIntoView({block:'center', inline:'nearest', behavior:'instant'});
return true;
})()
"""
)

BUTTON_CLICK = (
    "(function(){"
    + _FIND_CONTROLS
    + r"""
if (button.hidden || button.disabled) return false;
button.click();
return true;
})()
"""
)


def script_value(dialog, script):
    ok, value = dialog._run_script(script)
    if not ok:
        raise RuntimeError(f"WebView script failed: {value}")
    for _ in range(2):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            break
    return value


class RecordingBrowser(LCSCBrowser):
    """Keep the real page/native boundary, replacing only asset import work."""

    def __init__(self, project_dir):
        self.smoke_imports = []
        self.smoke_completions = 0
        super().__init__(project_dir=project_dir, initial_url=SEARCH_URL)
        self.SetTitle("立创元件桥 · 列表按钮检查")

    def _start_import(self, component_id):
        self.smoke_imports.append(component_id)
        self._busy = True
        self._active_component_id = component_id
        self._last_error = None
        self._set_page_state(component_id, "importing")
        # This never starts the production worker or writes component assets.
        wx.CallLater(350, self._smoke_complete, component_id)

    def _smoke_complete(self, component_id):
        if self._closed:
            return
        self._busy = False
        self.smoke_completions += 1
        self._set_page_state(component_id, "success", "已导入")


def assert_layout(button):
    assert isinstance(button, dict), "Result-row import button is missing"
    assert not button["hidden"] and button["rect"]["width"] > 0
    assert button["sameParent"], "Cart and import buttons left their shared operation container"
    cart, imported, parent = button["cart"]["rect"], button["rect"], button["parent"]
    assert imported["left"] > cart["right"], f"Import button is not to the cart's right: {button}"
    assert abs(imported["top"] - cart["top"]) <= 1, f"Buttons are not on the same row: {button}"
    assert abs(imported["height"] - cart["height"]) <= 1, f"Button heights differ: {button}"
    assert parent["left"] <= cart["left"] + 1 and parent["right"] >= imported["right"] - 1, (
        f"Buttons overflow their operation container: {button}"
    )
    assert imported["right"] <= button["viewport"] + 1, (
        f"Import button is outside the viewport: {button}"
    )
    assert button["cart"]["text"] == "加入购物车" and not button["cart"]["disabled"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hold", action="store_true", help="Keep the verified window open")
    parser.add_argument("--timeout", type=float, default=90, help="Page-load timeout in seconds")
    args = parser.parse_args()
    app = wx.App(False)
    wx.Log.SetActiveTarget(wx.LogStderr())
    try:
        with tempfile.TemporaryDirectory(prefix="lcsc-list-smoke-") as folder:
            project = Path(folder)
            (project / "smoke.kicad_pro").write_text("{}", encoding="utf-8")
            dialog = RecordingBrowser(project)
            state = {
                "phase": "page",
                "deadline": time.monotonic() + args.timeout,
                "inside_tick": False,
                "error": None,
                "passed": False,
                "messages": [],
                "baseline": None,
                "expected": 0,
            }
            timer = wx.Timer(dialog)

            def message_seen(event):
                if event.GetMessageHandler() == "lcscImport":
                    state["messages"].append(json.loads(event.GetString()))
                event.Skip()

            if dialog.webview:
                dialog.webview.Bind(html2.EVT_WEBVIEW_SCRIPT_MESSAGE_RECEIVED, message_seen)

            def finish():
                timer.Stop()
                # Only the inert mock can be busy; no production worker exists.
                dialog._busy = False
                if not dialog._closed:
                    dialog._on_close(wx.CloseEvent(wx.wxEVT_CLOSE_WINDOW))

            def assert_unchanged_cart(button):
                assert_layout(button)
                assert button["cartClicks"] == 0, "Import click reached a store cart button"
                for key in ("text", "disabled", "metadata"):
                    assert button["cart"][key] == state["baseline"][key], f"Cart {key} changed"

            def assert_delivery():
                expected = state["expected"]
                assert len(state["messages"]) == expected, (
                    "Each click must send exactly one native message"
                )
                assert dialog.smoke_imports == [COMPONENT] * expected, (
                    "Validated import target differs"
                )
                assert all(message["id"] == COMPONENT for message in state["messages"])
                assert {path.name for path in project.iterdir()} == {"smoke.kicad_pro"}, (
                    "The smoke test unexpectedly created project assets"
                )

            def click_button():
                state.update(
                    phase="click", expected=state["expected"] + 1, deadline=time.monotonic() + 15
                )
                assert script_value(dialog, BUTTON_CLICK) is True, "Import button was not clickable"

            def advance(event):
                # RunScript pumps wx events. Never nest script evaluations.
                if state["inside_tick"] or dialog._script_active or dialog._closed:
                    return
                state["inside_tick"] = True
                try:
                    if dialog.webview is None:
                        raise RuntimeError("Native WebView backend unavailable")
                    if time.monotonic() > state["deadline"]:
                        details = {
                            "phase": state["phase"],
                            "url": dialog.webview.GetCurrentURL(),
                            "loading": dialog._page_loading,
                            "status": dialog.status.GetLabel(),
                        }
                        for label, script in (
                            ("product", current_product_script(COMPONENT)),
                            ("button", BUTTON_STATE),
                        ):
                            try:
                                details[label] = script_value(dialog, script)
                            except RuntimeError as error:
                                details[label] = str(error)
                        raise TimeoutError(f"List smoke timed out: {details}")
                    if state["phase"] == "page":
                        if dialog._page_loading or dialog.webview.GetCurrentURL() != SEARCH_URL:
                            return
                        if not script_value(dialog, BUTTON_STATE):
                            return
                        assert script_value(dialog, current_product_script(COMPONENT)) == COMPONENT
                        assert script_value(dialog, ARM_CART_OBSERVER) is True
                        state.update(phase="quiet", quiet_until=time.monotonic() + 1.2)
                    elif state["phase"] == "quiet":
                        assert not state["messages"] and not dialog.smoke_imports, (
                            "Automatic import occurred"
                        )
                        if time.monotonic() < state["quiet_until"]:
                            return
                        button = script_value(dialog, BUTTON_STATE)
                        assert_layout(button)
                        assert not button["disabled"] and button["cartClicks"] == 0
                        state["baseline"] = button["cart"]
                        print(
                            "PASS: import button sits to the cart's right inside the same operation container",
                            flush=True,
                        )
                        click_button()
                    elif state["phase"] == "click":
                        assert len(dialog.smoke_imports) <= state["expected"], (
                            "Duplicate import request"
                        )
                        if len(dialog.smoke_imports) < state["expected"]:
                            return
                        assert_delivery()
                        button = script_value(dialog, BUTTON_STATE)
                        assert_unchanged_cart(button)
                        if dialog._busy:
                            assert button["disabled"] and button["state"] == "importing"
                            return
                        if dialog.smoke_completions != state["expected"]:
                            return
                        assert button["state"] == "success" and not button["disabled"]
                        assert button["text"] == "已导入"
                        state.update(phase="success", quiet_until=time.monotonic() + 1.2)
                    elif state["phase"] == "success":
                        assert_delivery()
                        button = script_value(dialog, BUTTON_STATE)
                        assert_unchanged_cart(button)
                        assert button["state"] == "success" and not button["disabled"]
                        if time.monotonic() < state["quiet_until"]:
                            return
                        if state["expected"] == 1:
                            print(
                                "PASS: one DOM click delivered one validated native C157624 message; cart untouched",
                                flush=True,
                            )
                            click_button()
                            return
                        state["passed"] = True
                        timer.Stop()
                        print(
                            "PASS: success is retryable; two clicks delivered exactly two messages and zero cart events",
                            flush=True,
                        )
                        print(
                            json.dumps(
                                {
                                    "url": dialog.webview.GetCurrentURL(),
                                    "component": COMPONENT,
                                    "native_messages": len(state["messages"]),
                                    "cart_clicks": button["cartClicks"],
                                    "cart": button["cart"]["rect"],
                                    "import": button["rect"],
                                    "state": button["state"],
                                }
                            ),
                            flush=True,
                        )
                        if args.hold:
                            dialog._set_page_state(COMPONENT, "idle")
                            dialog.status.SetLabel("")
                            assert script_value(dialog, ARM_CART_OBSERVER) is True
                            print("HOLD: close the browser window to exit", flush=True)
                        else:
                            finish()
                except Exception as error:
                    state["error"] = error
                    finish()
                finally:
                    state["inside_tick"] = False

            dialog.Bind(wx.EVT_TIMER, advance, timer)
            timer.Start(100)
            try:
                dialog.ShowModal()
            finally:
                timer.Stop()
                dialog.Destroy()
            if state["error"]:
                raise state["error"]
            if not state["passed"]:
                raise RuntimeError("Browser closed before the native list checks completed")
    finally:
        app.Destroy()


if __name__ == "__main__":
    main()
