"""Opt-in, offline native smoke test for the browser shell.

Run with KiCad's Python: python tools/smoke_lcsc_ui.py [--hold]
Uses the actual two local WebViews and native script-message handler. The store
area and owner actions are inert fixtures: no network access or imports occur.
With --hold, the passing success state remains visible until the window closes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

import wx  # noqa: E402
from lcsc_page import normalize_navigation  # noqa: E402
from lcsc_ui import BrowserChrome  # noqa: E402

SIDEBAR_STATE = r"""
(() => {
  const byId = id => document.getElementById(id);
  return JSON.stringify({
    ready: Boolean(window.LCSCBridgeUI),
    bootstrap: getComputedStyle(document.documentElement).getPropertyValue('--bs-blue').trim(),
    project: byId('project-name').textContent,
    path: byId('project-path').textContent,
    pathTitle: byId('project-path').title,
    tone: byId('status-card').dataset.tone,
    busy: byId('status-card').getAttribute('aria-busy'),
    message: byId('status-message').textContent,
    messageHidden: byId('status-message').hidden,
    spinnerVisible: getComputedStyle(document.querySelector('.spinner-border')).display !== 'none',
    resultHidden: byId('result-section').hidden,
    component: byId('result-component').textContent,
    library: byId('result-library').textContent,
    assetsHidden: byId('result-assets').hidden,
    modelHidden: byId('result-model').hidden,
    logCount: Number(byId('log-count').textContent),
    lastLog: byId('log-list').lastElementChild?.textContent || '',
    injectedElements: document.querySelectorAll('#status-message img, #log-list img').length,
    width: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    bodyScrollWidth: document.body.scrollWidth
  });
})()
"""

TOOLBAR_STATE = r"""
(() => {
  const input = document.getElementById('address-input');
  const controls = [...document.querySelectorAll('.browser-toolbar button, #address-input')]
    .filter(node => getComputedStyle(node).display !== 'none')
    .map(node => {
      const r = node.getBoundingClientRect();
      return {left:r.left, right:r.right, top:r.top, bottom:r.bottom, width:r.width};
    });
  return JSON.stringify({
    ready: Boolean(window.LCSCBridgeUI),
    address: input.value,
    focused: document.activeElement === input,
    chooseDisabled: document.querySelector('[data-action="choose-project"]').disabled,
    status: document.getElementById('toolbar-status').textContent.trim(),
    statusHidden: document.getElementById('toolbar-status').hidden,
    panelExpanded: document.getElementById('details-toggle').getAttribute('aria-expanded'),
    width: innerWidth, height: innerHeight,
    scrollWidth: document.documentElement.scrollWidth,
    inputWidth: input.getBoundingClientRect().width,
    controls
  });
})()
"""


class OfflineOwner(wx.Dialog):
    """The real chrome with inert business callbacks and no remote WebView."""

    def __init__(self):
        super().__init__(
            None,
            title="立创元件桥 · 离线界面检查",
            size=(1380, 900),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )
        self.SetMinSize(self.FromDIP((1060, 700)))
        self._closed = self._busy = self._script_active = False
        self._pending_page_message = self._pending_page_state = None
        self._script_replay_scheduled = False
        self.actions = []
        self.webview = SimpleNamespace(
            CanGoBack=lambda: True,
            CanGoForward=lambda: True,
            GoBack=lambda: self.actions.append(("back", "")),
            GoForward=lambda: self.actions.append(("forward", "")),
            LoadURL=lambda url: self.actions.append(("load", url)),
            Reload=lambda: self.actions.append(("reload", "")),
        )
        self.SetBackgroundColour(wx.Colour("#ffffff"))
        outer = wx.BoxSizer(wx.VERTICAL)
        toolbar = wx.Panel(self)
        toolbar.SetMinSize(self.FromDIP((-1, 52)))
        body = wx.Panel(self)
        sidebar = self.details_panel = wx.Panel(body)
        sidebar.SetMinSize(self.FromDIP((280, -1)))
        sidebar.Hide()
        content = self.store_area = wx.Panel(body)
        self.chrome = BrowserChrome(self, sidebar, toolbar)
        for panel, view in ((sidebar, self.chrome.sidebar), (toolbar, self.chrome.toolbar)):
            layout = wx.BoxSizer(wx.VERTICAL)
            layout.Add(view, 1, wx.EXPAND)
            panel.SetSizer(layout)
        self.address = self.chrome.control("address")
        self.status = self.chrome.control("status")
        self.log = self.chrome.control("logs")
        self.project_button = self.chrome.control("projectEnabled")
        layout = wx.BoxSizer(wx.VERTICAL)
        placeholder = wx.Panel(content)
        placeholder.SetBackgroundColour(wx.Colour("#ffffff"))
        placeholder_layout = wx.BoxSizer(wx.VERTICAL)
        placeholder_layout.AddStretchSpacer()
        label = wx.StaticText(placeholder, label="离线界面检查\n商品区域未连接网络")
        label.SetForegroundColour(wx.Colour("#75837d"))
        placeholder_layout.Add(label, 0, wx.ALIGN_CENTER | wx.ALL, 24)
        placeholder_layout.AddStretchSpacer()
        placeholder.SetSizer(placeholder_layout)
        layout.Add(placeholder, 1, wx.EXPAND)
        content.SetSizer(layout)
        body_layout = wx.BoxSizer(wx.HORIZONTAL)
        body_layout.Add(content, 1, wx.EXPAND)
        body_layout.Add(sidebar, 0, wx.EXPAND)
        body.SetSizer(body_layout)
        outer.Add(toolbar, 0, wx.EXPAND)
        outer.Add(body, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_BUTTON, self._on_close, id=wx.ID_CANCEL)
        self.Centre()

    def _choose_project(self, event):
        self.actions.append(("choose-project", ""))

    def _toggle_details(self, event=None):
        opened = not self.chrome.model["panelOpen"]
        self.details_panel.Show(opened)
        self.chrome.set("panelOpen", opened)
        self.Layout()

    def _navigate(self, event):
        self.actions.append(("navigate", normalize_navigation(self.address.GetValue())))

    def _replay_script_work(self):
        raise AssertionError("The offline shell must not generate a product-import message")

    def _on_close(self, event):
        if not self._closed:
            self._closed = True
            self.chrome.close()
            self.EndModal(wx.ID_CANCEL)


class SmokeRunner:
    def __init__(self, owner, hold):
        self.owner, self.hold = owner, hold
        self.error = None
        self.checks = []
        self.inside_tick = False
        self.steps = self.run_checks()
        self.timer = wx.Timer(owner)
        owner.Bind(wx.EVT_TIMER, self.advance, self.timer)
        self.timer.Start(80)

    def report(self, name):
        self.checks.append(name)
        print(json.dumps({"check": name, "result": "passed"}), flush=True)

    def script(self, view, script):
        owner = self.owner
        assert not owner._script_active, "Native WebView evaluations must not nest"
        owner._script_active = True
        try:
            ok, value = view.RunScript(script)
        finally:
            owner._script_active = False
        if not ok:
            raise RuntimeError(f"Native shell script failed: {value}")
        for _ in range(2):
            if not isinstance(value, str):
                break
            try:
                value = json.loads(value)
            except ValueError:
                break
        return value

    def wait_until(self, condition, description, timeout=12):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                raise TimeoutError(f"Offline UI timed out waiting for {description}")
            yield

    def settle(self):
        deadline = time.monotonic() + 0.25
        yield from self.wait_until(lambda: time.monotonic() >= deadline, "native messages")

    def sidebar(self):
        self.owner.chrome.flush()
        return self.script(self.owner.chrome.sidebar, SIDEBAR_STATE)

    def toolbar(self):
        self.owner.chrome.flush()
        return self.script(self.owner.chrome.toolbar, TOOLBAR_STATE)

    def run_checks(self):
        owner, chrome = self.owner, self.owner.chrome
        yield from self.wait_until(lambda: len(chrome.ready) == 2, "both local HTML documents")
        side, top = self.sidebar(), self.toolbar()
        assert side["ready"] and top["ready"]
        assert side["bootstrap"] == "#0d6efd", "Bundled Bootstrap stylesheet did not load"
        assert not chrome.model["projectPath"] and side["tone"] == "idle"
        assert side["resultHidden"] and not top["chooseDisabled"]
        assert not chrome.model["panelOpen"] and not owner.details_panel.IsShown()
        assert top["panelExpanded"] == "false" and top["statusHidden"]
        assert not side["message"] and not chrome.model["status"]
        assert chrome.toolbar.GetSize().height == owner.FromDIP(52)
        assert chrome.toolbar.GetSize().width == owner.GetClientSize().width
        assert owner.store_area.GetSize().width == owner.GetClientSize().width
        assert not owner.actions
        self.report("local assets and empty project")

        for opened in (True, False, True):
            self.script(chrome.toolbar, "document.getElementById('details-toggle').click()")
            yield from self.wait_until(
                lambda: chrome.model["panelOpen"] is opened, "native details toggle"
            )
            assert owner.details_panel.IsShown() is opened
            assert self.toolbar()["panelExpanded"] == str(opened).lower()
            remaining_width = owner.GetClientSize().width - owner.store_area.GetSize().width
            assert remaining_width == (owner.FromDIP(280) if opened else 0)
            if opened:
                assert owner.details_panel.GetPosition().x == owner.store_area.GetSize().width
        self.report("collapsed default and native details toggle")

        long_path = "D:\\工程\\" + "很长的目录名\\" * 12 + "控制板工程"
        chrome.set("projectPath", long_path)
        side = self.sidebar()
        assert side["path"] == long_path and side["pathTitle"] == long_path
        assert side["project"] == "控制板工程"
        self.report("complete state and long project path")

        owner._busy = True
        owner.project_button.Disable()
        chrome.set_import_state("busy", component_id="C2040")
        owner.status.SetLabel("正在导入 C2040")
        side, top = self.sidebar(), self.toolbar()
        assert side["tone"] == "busy" and side["busy"] == "true"
        assert top["chooseDisabled"] and side["spinnerVisible"]
        assert top["status"] == "C2040 · 导入中" and not top["statusHidden"]
        assert side["component"] == "C2040" and side["assetsHidden"]
        self.script(
            chrome.toolbar, "document.querySelector('[data-action=\"choose-project\"]').click()"
        )
        self.script(
            chrome.toolbar,
            "window.lcscShell.postMessage(JSON.stringify({version:1,action:'choose-project'}))",
        )
        yield from self.settle()
        chrome.flush()
        assert not owner.actions, "Busy imports must prevent both DOM and forged project changes"
        self.report("busy presentation and guarded project selection")

        owner._busy = False
        owner.project_button.Enable()
        result = SimpleNamespace(
            component_id="C2040", library_name="LCSC", has_3d=True, already_present=False
        )
        chrome.set_import_state("success", result=result)
        owner.status.SetLabel("C2040 已导入")
        for index in range(180):
            owner.log.AppendText(f"步骤 {index:03d}：工程库检查完成\n")
        side, top = self.sidebar(), self.toolbar()
        assert side["tone"] == "success" and side["library"] == "LCSC"
        assert top["status"] == "C2040 · 已导入" and not top["statusHidden"]
        assert side["messageHidden"], "Do not repeat the success summary"
        assert not side["assetsHidden"] and not side["modelHidden"]
        assert side["logCount"] == 160 and "179" in side["lastLog"]
        owner.status.SetLabel("请在外部浏览器打开此链接")
        side, top = self.sidebar(), self.toolbar()
        assert not side["messageHidden"] and top["status"] == side["message"]
        owner.status.SetLabel("C2040 已导入")
        self.report("success assets and bounded activity history")

        hostile_text = '<img src="https://example.invalid/never-requested">导入失败'
        owner._toggle_details()
        assert not owner.details_panel.IsShown()
        chrome.set_import_state("error")
        owner.status.SetLabel(hostile_text)
        owner.log.AppendText(hostile_text + "\n")
        side = self.sidebar()
        assert side["tone"] == "error" and side["assetsHidden"]
        assert side["message"] == hostile_text and side["lastLog"] == hostile_text
        assert side["injectedElements"] == 0, "Status and logs must render plain text"
        assert chrome.model["panelOpen"] and owner.details_panel.IsShown()
        self.report("error state and literal untrusted text")

        chrome.toolbar.SetFocus()
        self.script(
            chrome.toolbar,
            "(() => {const input=document.getElementById('address-input'); input.focus();"
            "input.value='C99999'; input.dispatchEvent(new Event('input',{bubbles:true}));})()",
        )
        chrome.set("address", "https://item.szlcsc.com/2392.html")
        top = self.toolbar()
        assert top["focused"] and top["address"] == "C99999", "Refresh overwrote an edited address"
        self.script(chrome.toolbar, "document.getElementById('address-form').requestSubmit()")
        yield from self.wait_until(lambda: bool(owner.actions), "native navigation message")
        assert owner.actions == [("navigate", normalize_navigation("C99999"))]
        self.report("address editing and native submit message")

        self.script(
            chrome.toolbar, "document.querySelector('[data-action=\"choose-project\"]').click()"
        )
        yield from self.wait_until(lambda: len(owner.actions) == 2, "native project button message")
        assert owner.actions[-1] == ("choose-project", "")
        invalid = [
            "not-json",
            json.dumps({"version": 1, "action": "import", "id": "C2040"}),
            json.dumps({"version": 1, "action": "choose-project", "path": "D:/wrong-project"}),
        ]
        self.script(
            chrome.sidebar,
            "(() => {for(const raw of "
            + json.dumps(invalid)
            + ") window.lcscShell.postMessage(raw);})()",
        )
        yield from self.settle()
        chrome.flush()
        assert len(owner.actions) == 2, "Invalid shell commands reached an owner callback"
        self.report("native project button and rejected invalid messages")

        owner.SetSize(owner.FromDIP((1060, 700)))
        owner.Layout()
        self.script(chrome.sidebar, "document.querySelector('.log-panel').open=true")
        yield from self.settle()
        side, top = self.sidebar(), self.toolbar()
        assert max(side["scrollWidth"], side["bodyScrollWidth"]) <= side["width"] + 1
        assert top["scrollWidth"] <= top["width"] + 1 and top["inputWidth"] >= 80
        for bounds in top["controls"]:
            assert bounds["left"] >= -1 and bounds["right"] <= top["width"] + 1, bounds
            assert bounds["top"] >= -1 and bounds["bottom"] <= top["height"] + 1, bounds
        self.report("minimum window geometry and scrollable activity history")

        chrome.set("projectPath", "D:\\工程\\控制板工程")
        chrome.set_import_state("success", result=result)
        owner.status.SetLabel("C2040 已导入")
        chrome.set("logs", ["C2040 已导入当前工程", "符号、封装与 3D 模型已就位"])
        self.script(chrome.sidebar, "document.querySelector('.log-panel').open=false")
        chrome.flush()
        print(
            json.dumps(
                {"result": "passed", "checks": len(self.checks), "offline": True, "hold": self.hold}
            ),
            flush=True,
        )

    def advance(self, event):
        if self.inside_tick or self.owner._script_active:
            return
        self.inside_tick = True
        try:
            next(self.steps)
        except StopIteration:
            self.timer.Stop()
            if not self.hold:
                wx.CallAfter(self.owner.Close)
        except Exception as error:
            self.error = error
            self.timer.Stop()
            wx.CallAfter(self.owner.Close)
        finally:
            self.inside_tick = False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hold", action="store_true", help="Keep the passing success state visible"
    )
    args = parser.parse_args()
    app = wx.App(False)
    wx.Log.SetActiveTarget(wx.LogStderr())
    dialog = OfflineOwner()
    runner = SmokeRunner(dialog, args.hold)
    try:
        dialog.ShowModal()
    finally:
        runner.timer.Stop()
        if not dialog.chrome.closed:
            dialog.chrome.close()
        dialog.Destroy()
        app.Destroy()
    if runner.error is not None:
        raise runner.error
    if len(runner.checks) != 9:
        raise RuntimeError("Offline UI smoke was closed before all checks completed")


if __name__ == "__main__":
    main()
