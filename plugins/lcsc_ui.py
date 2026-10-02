"""Local browser chrome around the separate, untrusted store WebView."""

from __future__ import annotations

import json
from pathlib import Path

import wx
import wx.html2 as html2

UI_ROOT = Path(__file__).resolve().parent / "ui"
MAX_MESSAGE = 8192
_ACTIONS = {
    "ready",
    "navigate",
    "back",
    "forward",
    "home",
    "reload",
    "choose-project",
    "toggle-panel",
}


def parse_shell_message(raw):
    """The local shell controls browsing and project selection, never imports."""
    if not isinstance(raw, str) or len(raw) > MAX_MESSAGE:
        raise ValueError("Invalid shell message")
    try:
        message = json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise ValueError("Invalid shell JSON") from error
    if not isinstance(message, dict):
        raise ValueError("Invalid shell object")
    action = message.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        raise ValueError("Unknown shell action")
    keys = {"version", "action", "value"} if action == "navigate" else {"version", "action"}
    if set(message) != keys or type(message["version"]) is not int or message["version"] != 1:
        raise ValueError("Invalid shell fields")
    if action == "navigate" and (
        not isinstance(message["value"], str) or len(message["value"]) > 4096
    ):
        raise ValueError("Invalid navigation value")
    return action, message.get("value", "")


def webview_backend():
    import sys

    if sys.platform == "win32":
        backend = html2.WebViewBackendEdge
        if not html2.WebView.IsBackendAvailable(backend):
            raise RuntimeError("未检测到 Microsoft Edge WebView2 Runtime，请安装后重新打开插件。")
        return backend
    return html2.WebViewBackendDefault


class UIControl:
    """Small control adapter: update memory now, render outside event callbacks."""

    def __init__(self, chrome, key, value=""):
        self.chrome, self.key = chrome, key
        self.value = value

    def ChangeValue(self, value):
        self.value = str(value)
        self.chrome.set(self.key, self.value)

    SetLabel = ChangeValue

    def GetValue(self):
        return self.value

    GetLabel = GetValue

    def SetToolTip(self, value):
        pass  # Full project path is available in the HTML title and details.

    def Enable(self, enabled=True):
        self.chrome.set(self.key, bool(enabled))

    def Disable(self):
        self.Enable(False)

    def AppendText(self, value):
        self.value = (self.value + str(value))[-30000:]
        self.chrome.set("logs", self.value.splitlines()[-160:])


class BrowserChrome:
    """Two trusted local views sharing one small, versioned presentation model."""

    def __init__(self, owner, sidebar_parent, toolbar_parent):
        self.owner = owner
        self.model = {
            "projectPath": "",
            "projectName": "",
            "address": "",
            "canBack": False,
            "canForward": False,
            "busy": False,
            "panelOpen": False,
            "status": "",
            "statusTone": "idle",
            "lastResult": None,
            "logs": [],
        }
        self.views = []
        self.ready = set()
        self.dirty = True
        self.evaluating = False
        self.flushing = False
        self.pending_actions = []
        self.closed = False
        self._urls = {}
        for mode, parent in (("sidebar", sidebar_parent), ("toolbar", toolbar_parent)):
            view = html2.WebView.New(parent, backend=webview_backend())
            if not view or not view.AddScriptMessageHandler("lcscShell"):
                raise RuntimeError("界面加载失败，请更新 WebView2 后重试。")
            view.SetBackgroundColour(wx.Colour("#ffffff"))
            self.views.append(view)
            self._urls[view] = (UI_ROOT / "index.html").as_uri() + "#" + mode
            view.Bind(
                html2.EVT_WEBVIEW_SCRIPT_MESSAGE_RECEIVED,
                lambda event, source=view: self.on_message(source, event),
            )
            view.Bind(
                html2.EVT_WEBVIEW_NAVIGATING,
                lambda event, source=view: self.on_navigating(source, event),
            )
            view.Bind(html2.EVT_WEBVIEW_NEWWINDOW, lambda event: event.Veto())
            view.Bind(
                html2.EVT_WEBVIEW_LOADED, lambda event, source=view: self.on_loaded(source, event)
            )
            view.LoadURL(self._urls[view])
        self.sidebar, self.toolbar = self.views
        self.timer = wx.Timer(owner)
        owner.Bind(wx.EVT_TIMER, self.flush, self.timer)
        self.timer.Start(100)

    def control(self, key, value=""):
        return UIControl(self, key, value)

    def set(self, key, value):
        if key == "projectPath":
            self.model["projectName"] = Path(value).name if value else ""
        if key == "projectEnabled":
            key, value = "busy", not value
        if self.model.get(key) != value:
            self.model[key] = value
            self.dirty = True

    def set_import_state(self, tone, result=None, component_id=None):
        self.set("statusTone", tone)
        if tone == "error" and not self.model.get("panelOpen"):
            self.owner._toggle_details()
        if tone == "idle":
            self.set("lastResult", None)
        elif tone == "busy":
            self.set(
                "lastResult",
                {
                    "componentId": component_id,
                    "libraryName": "",
                    "has3d": False,
                    "alreadyPresent": False,
                },
            )
        elif result is not None:
            self.set(
                "lastResult",
                {
                    "componentId": result.component_id,
                    "libraryName": result.library_name,
                    "has3d": result.has_3d,
                    "alreadyPresent": result.already_present,
                },
            )

    def trusted(self, view):
        return not self.closed and view.GetCurrentURL() == self._urls.get(view)

    def on_navigating(self, view, event):
        if event.GetURL() != self._urls[view] and event.GetURL() != "about:blank":
            event.Veto()

    def on_loaded(self, view, event):
        if self.trusted(view) and event.GetURL() == self._urls[view]:
            self.ready.add(view)
            self.dirty = True

    def on_message(self, view, event):
        if not self.trusted(view) or event.GetMessageHandler() != "lcscShell":
            return
        try:
            action, value = parse_shell_message(event.GetString())
        except ValueError:
            return
        if action == "ready":
            self.ready.add(view)
            self.dirty = True
        elif len(self.pending_actions) < 8:
            # Native RunScript pumps window events; never execute nested UI work.
            self.pending_actions.append((view, action, value))

    def flush(self, event=None):
        if self.closed or self.owner._closed or self.flushing or self.owner._script_active:
            return
        self.flushing = True
        try:
            self._flush()
        finally:
            self.flushing = False

    def _flush(self):
        while self.pending_actions and not self.closed:
            view, action, value = self.pending_actions.pop(0)
            if self.trusted(view):
                self._dispatch(action, value)
        if not self.dirty or self.closed:
            return
        self.dirty = False
        payload = json.dumps(self.model, ensure_ascii=True)
        script = "Boolean(window.LCSCBridgeUI && window.LCSCBridgeUI.update(" + payload + "))"
        self.evaluating = True
        # Share the remote view's guard: either view can pump a click from another.
        self.owner._script_active = True
        try:
            for view in tuple(self.ready):
                if self.closed or not self.trusted(view):
                    continue
                ok, _ = view.RunScript(script)
                if not ok:
                    self.ready.discard(view)
        finally:
            self.evaluating = False
            self.owner._script_active = False
            if (
                not self.closed
                and (
                    self.owner._pending_page_message is not None
                    or self.owner._pending_page_state is not None
                )
                and not self.owner._script_replay_scheduled
            ):
                self.owner._script_replay_scheduled = True
                wx.CallAfter(self.owner._replay_script_work)

    def _dispatch(self, action, value):
        owner = self.owner
        if owner._closed:
            return
        if action == "choose-project":
            if not owner._busy:
                owner._choose_project(None)
        elif action == "toggle-panel":
            owner._toggle_details()
        elif action == "navigate":
            owner.address.ChangeValue(value.strip())
            owner._navigate(None)
        elif owner.webview:
            if action == "back" and owner.webview.CanGoBack():
                owner.webview.GoBack()
            elif action == "forward" and owner.webview.CanGoForward():
                owner.webview.GoForward()
            elif action == "home":
                from lcsc_page import LCSC_HOME

                owner.webview.LoadURL(LCSC_HOME)
            elif action == "reload":
                owner.webview.Reload()

    def close(self):
        self.closed = True
        self.pending_actions.clear()
        self.ready.clear()
        self.timer.Stop()
