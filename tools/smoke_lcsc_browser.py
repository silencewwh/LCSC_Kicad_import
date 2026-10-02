"""Opt-in native page-button -> wx message -> live import integration check.

Run with KiCad's Python: python tools/smoke_lcsc_browser.py
The browser clicks the injected DOM button; no import method is called directly.
Only a temporary project is changed, including the repeat-import check.
"""

from __future__ import annotations

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

BUTTON_STATE = """
(function(){var b=document.getElementById('lcsc-kicad-import');
return JSON.stringify(b ? {hidden:b.hidden,disabled:b.disabled,text:b.textContent,title:b.title} : null);})()
"""
# A synchronous click can deliver a native message while RunScript pumps events.
# The production wrapper must defer that message until this evaluation returns.
BUTTON_CLICK = """
(function(){var b=document.getElementById('lcsc-kicad-import');
if(!b || b.hidden || b.disabled) return false; b.click(); return true;})()
"""


def script_value(dialog, script):
    ok, value = dialog._run_script(script)
    if not ok:
        raise RuntimeError(f"WebView script failed: {value}")
    # wx backends may return either the serialized JS value or its string value.
    for _ in range(2):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            break
    return value


def main():
    app = wx.App(False)
    wx.Log.SetActiveTarget(wx.LogStderr())
    with tempfile.TemporaryDirectory(prefix="lcsc-browser-smoke-") as folder:
        project = Path(folder)
        (project / "smoke.kicad_pro").write_text("{}", encoding="utf-8")
        dialog = LCSCBrowser(project_dir=project, initial_url="https://item.szlcsc.com/2392.html")
        state = {
            "phase": "page",
            "deadline": time.monotonic() + 60,
            "error": None,
            "messages": 0,
            "inside_tick": False,
        }
        timer = wx.Timer(dialog)

        def message_seen(event):
            if event.GetMessageHandler() == "lcscImport":
                state["messages"] += 1
            event.Skip()

        if dialog.webview:
            dialog.webview.Bind(html2.EVT_WEBVIEW_SCRIPT_MESSAGE_RECEIVED, message_seen)

        def finish():
            timer.Stop()
            dialog._on_close(wx.CloseEvent(wx.wxEVT_CLOSE_WINDOW))

        def click_button(phase):
            state.update(phase=phase, deadline=time.monotonic() + 180)
            assert script_value(dialog, BUTTON_CLICK) is True, (
                "Injected DOM button was not clickable"
            )

        def advance(event):
            # RunScript pumps native events and can re-enter this timer callback.
            if state["inside_tick"] or dialog._script_active:
                return
            state["inside_tick"] = True
            try:
                if state["error"]:
                    if not dialog._busy:
                        finish()
                    return
                if time.monotonic() > state["deadline"]:
                    raise TimeoutError(
                        f"Native browser timed out during {state['phase']}; "
                        f"url={dialog.webview.GetCurrentURL()}; loading={dialog._page_loading}; "
                        f"busy={dialog.webview.IsBusy()}; "
                        f"product={script_value(dialog, current_product_script())!r}; "
                        f"button={script_value(dialog, BUTTON_STATE)!r}; {dialog.status.GetLabel()}"
                    )
                if dialog.webview is None:
                    raise RuntimeError("Native WebView backend unavailable")
                if state["phase"] == "page":
                    if dialog._page_loading:
                        return
                    if not dialog.webview.GetCurrentURL().startswith(
                        "https://item.szlcsc.com/2392.html"
                    ):
                        return
                    button = script_value(dialog, BUTTON_STATE)
                    if (
                        isinstance(button, dict)
                        and not button["hidden"]
                        and "C2040" in button["title"]
                    ):
                        assert not button["disabled"]
                        state.update(phase="quiet", quiet_until=time.monotonic() + 1.2)
                elif state["phase"] == "quiet":
                    assert not dialog._busy and dialog._last_result is None
                    assert state["messages"] == 0 and not (project / "LCSC").exists()
                    if time.monotonic() >= state["quiet_until"]:
                        print(
                            "PASS: Chinese product page shows in-page button; no automatic import",
                            flush=True,
                        )
                        click_button("click")
                elif state["phase"] == "click":
                    if dialog._busy or dialog._last_result is not None or dialog._last_error:
                        assert state["messages"] == 1, (
                            "Import did not come through the native page-message event"
                        )
                        # A cached import can finish before the next native timer tick.
                        if dialog._busy:
                            button = script_value(dialog, BUTTON_STATE)
                            assert button["disabled"]
                            close_event = wx.CloseEvent(wx.wxEVT_CLOSE_WINDOW)
                            close_event.SetCanVeto(True)
                            dialog._on_close(close_event)
                            assert close_event.GetVeto() and not dialog._closed
                        print(
                            "PASS: DOM click delivered native message and started import",
                            flush=True,
                        )
                        state["phase"] = "import"
                elif state["phase"] == "import" and not dialog._busy:
                    result = dialog._last_result
                    if result is None:
                        raise RuntimeError(dialog._last_error or dialog.log.GetValue())
                    assert result.component_id == "C2040" and not result.already_present
                    assert result.directory == project / "LCSC"
                    assert result.library_name == "LCSC"
                    assert (result.directory / "LCSC.kicad_sym").is_file()
                    for name in ("sym-lib-table", "fp-lib-table"):
                        assert "${KIPRJMOD}/LCSC/" in (project / name).read_text(encoding="utf-8")
                    button = script_value(dialog, BUTTON_STATE)
                    assert not button["disabled"]
                    print(
                        "PASS: live assets downloaded and both project library tables registered",
                        flush=True,
                    )
                    print(
                        json.dumps(
                            {
                                "component": result.component_id,
                                "url": dialog.webview.GetCurrentURL(),
                                "has_3d": result.has_3d,
                                "status": dialog.status.GetLabel(),
                            },
                            ensure_ascii=True,
                        ),
                        flush=True,
                    )
                    click_button("repeat")
                elif state["phase"] == "repeat" and not dialog._busy:
                    if dialog._last_error:
                        raise RuntimeError(dialog._last_error)
                    result = dialog._last_result
                    if result is None:
                        return  # The native click event has not been delivered yet.
                    if not result.already_present:
                        return
                    assert state["messages"] == 2
                    assert result.component_id == "C2040" and dialog._last_error is None
                    assert "已存在" in dialog.status.GetLabel()
                    assert not script_value(dialog, BUTTON_STATE)["disabled"]
                    print(
                        "PASS: second DOM click retained existing assets and restored the button",
                        flush=True,
                    )
                    finish()
            except Exception as error:
                state["error"] = error
                if not dialog._busy:
                    finish()
            finally:
                state["inside_tick"] = False

        dialog.Bind(wx.EVT_TIMER, advance, timer)
        timer.Start(100)
        dialog.ShowModal()
        dialog.Destroy()
        if state["error"]:
            raise state["error"]
    app.Destroy()


if __name__ == "__main__":
    main()
