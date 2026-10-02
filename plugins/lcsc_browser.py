"""KiCad toolbar action: browse LCSC and install parts into the selected project."""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

import wx

PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from lcsc_page import (  # noqa: E402
    LCSC_HOME,
    normalize_component_id,
    normalize_navigation,
)
from lcsc_project_import import import_component_to_project  # noqa: E402
from lcsc_web_bridge import (  # noqa: E402
    current_product_script,
    injection_script,
    parse_import_message,
    refresh_script,
    status_script,
)

HOME_URL = LCSC_HOME


def _is_main_frame(event) -> bool:
    """Prefer the backend's frame identity; older wx only exposes target names."""
    if hasattr(event, "IsTargetMainFrame"):
        return bool(event.IsTargetMainFrame())
    return event.GetTarget() in ("", "_self")


def detect_project() -> Path | None:
    """Use the launching editor's IPC documents and validated KIPRJMOD context."""
    if not os.environ.get("KICAD_API_SOCKET"):
        return None
    try:
        from kipy import KiCad
        from kipy.proto.common.types import DocumentType

        client = KiCad(timeout_ms=1500)
        projects = set()
        project_hint = Path(os.environ.get("KIPRJMOD", ""))
        if not (
            project_hint.is_absolute()
            and project_hint.is_dir()
            and any(project_hint.glob("*.kicad_pro"))
        ):
            project_hint = None
        for doc_type in (DocumentType.DOCTYPE_PCB, DocumentType.DOCTYPE_SCHEMATIC):
            try:
                documents = client.get_open_documents(doc_type)
            except Exception:
                # An editor may only implement its own document type.
                continue
            for document in documents:
                if document.project.path:
                    candidate = Path(document.project.path).resolve()
                    if candidate.is_dir() and any(candidate.glob("*.kicad_pro")):
                        projects.add(candidate)
                elif doc_type == DocumentType.DOCTYPE_SCHEMATIC:
                    # KiCad 10.0.6 sends only a basename here. Its project loader
                    # also sets KIPRJMOD, inherited by the IPC action process.
                    # Require a matching schematic file before using that hint.
                    schematic = Path(document.board_filename)
                    if (
                        project_hint is not None
                        and schematic.name == str(schematic)
                        and not schematic.is_absolute()
                    ):
                        schematic = project_hint / schematic
                    if (
                        schematic.is_absolute()
                        and schematic.suffix.lower() == ".kicad_sch"
                        and schematic.is_file()
                    ):
                        candidate = schematic.resolve().parent
                        if any(candidate.glob("*.kicad_pro")):
                            projects.add(candidate)
        return projects.pop() if len(projects) == 1 else None
    except Exception:
        logging.exception("Could not identify the launching KiCad project")
        return None


class LCSCBrowser(wx.Dialog):
    def __init__(self, parent=None, project_dir: Path | None = None, initial_url=HOME_URL):
        super().__init__(
            parent,
            title="立创元件桥 · LCSC KiCad Bridge",
            size=(1380, 900),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX | wx.MINIMIZE_BOX,
        )
        self.SetMinSize(self.FromDIP((1060, 700)))
        self._busy = False
        self._closed = False
        self._last_url = ""
        self._page_loading = False
        self._page_token = secrets.token_urlsafe(24)
        self._validating_click = False
        self._script_active = False
        self._script_replay_scheduled = False
        self._pending_page_message = None
        self._pending_page_state = None
        self._active_component_id = None
        self._last_result = None
        self._last_error = None
        self._project = Path(project_dir).resolve() if project_dir else None
        self.webview = None
        self._build_ui()
        self._set_project(self._project)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        # Escape must obey the same in-progress protection as the window close button.
        self.Bind(wx.EVT_BUTTON, self._on_close, id=wx.ID_CANCEL)
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._poll_page, self._timer)
        self._timer.Start(1000)
        self.Centre()
        if self.webview:
            self.webview.LoadURL(normalize_navigation(initial_url))

    def _build_ui(self):
        from lcsc_ui import BrowserChrome, webview_backend

        self.SetBackgroundColour(wx.Colour("#ffffff"))
        outer = wx.BoxSizer(wx.VERTICAL)
        toolbar = wx.Panel(self)
        toolbar.SetMinSize(self.FromDIP((-1, 52)))
        body = wx.Panel(self)
        body_layout = wx.BoxSizer(wx.HORIZONTAL)
        sidebar = self.details_panel = wx.Panel(body)
        sidebar.SetMinSize(self.FromDIP((280, -1)))
        sidebar.Hide()
        content = wx.Panel(body)
        content.SetBackgroundColour(wx.Colour("#ffffff"))
        self.chrome = BrowserChrome(self, sidebar, toolbar)
        for panel, view in ((sidebar, self.chrome.sidebar), (toolbar, self.chrome.toolbar)):
            layout = wx.BoxSizer(wx.VERTICAL)
            layout.Add(view, 1, wx.EXPAND)
            panel.SetSizer(layout)
        self.project_text = self.chrome.control("projectPath")
        self.project_button = self.chrome.control("projectEnabled")
        self.address = self.chrome.control("address", HOME_URL)
        self.back = self.chrome.control("canBack")
        self.forward = self.chrome.control("canForward")
        self.status = self.chrome.control("status")
        self.log = self.chrome.control("logs")
        self.address.ChangeValue(HOME_URL)
        layout = wx.BoxSizer(wx.VERTICAL)

        try:
            import wx.html2 as html2

            self.webview = html2.WebView.New(content, backend=webview_backend())
            if not self.webview:
                raise RuntimeError("当前 wxPython 不支持内嵌浏览器。")
            if not self.webview.AddScriptMessageHandler("lcscImport"):
                raise RuntimeError("当前浏览器不支持页面导入按钮，请更新 WebView2 或 wxPython。")
            self.webview.Bind(html2.EVT_WEBVIEW_SCRIPT_MESSAGE_RECEIVED, self._on_page_import)
            self.webview.Bind(html2.EVT_WEBVIEW_NAVIGATING, self._on_navigating)
            self.webview.Bind(html2.EVT_WEBVIEW_NAVIGATED, self._on_navigated)
            self.webview.Bind(html2.EVT_WEBVIEW_LOADED, self._on_loaded)
            self.webview.Bind(html2.EVT_WEBVIEW_NEWWINDOW, self._on_new_window)
            self.webview.Bind(html2.EVT_WEBVIEW_ERROR, self._on_web_error)
            layout.Add(self.webview, 1, wx.EXPAND)
        except Exception as error:
            logging.exception("Embedded browser unavailable")
            if self.webview:
                self.webview.Destroy()
                self.webview = None
            message = wx.StaticText(content, label=f"浏览器暂不可用\n\n{error}")
            message.Wrap(self.FromDIP(600))
            layout.Add(message, 1, wx.EXPAND | wx.ALL, self.FromDIP(32))
            self.chrome.set_import_state("error")
            self.status.SetLabel(str(error))

        content.SetSizer(layout)
        body_layout.Add(content, 1, wx.EXPAND)
        body_layout.Add(sidebar, 0, wx.EXPAND)
        body.SetSizer(body_layout)
        outer.Add(toolbar, 0, wx.EXPAND)
        outer.Add(body, 1, wx.EXPAND)
        self.SetSizer(outer)

    def _toggle_details(self, event=None):
        if self._closed:
            return
        opened = not self.chrome.model["panelOpen"]
        self.details_panel.Show(opened)
        self.chrome.set("panelOpen", opened)
        self.Layout()

    def _set_project(self, path):
        previous = self._project
        self._project = path
        self.project_text.ChangeValue(str(path) if path else "")
        self.project_text.SetToolTip(self.project_text.GetValue())
        if path == previous:
            return
        self._last_result = None
        self._last_error = None
        self._active_component_id = None
        self.status.SetLabel("")
        self.chrome.set_import_state("idle")
        if previous is not None:
            # Results belong to the project. Rebuild the page controls and reject
            # clicks queued for the previous target; keep the first picker click valid.
            self._page_token = secrets.token_urlsafe(24)
            self._pending_page_message = None
            self._pending_page_state = None
            self._install_page_button()

    def _choose_project(self, event):
        if self._busy or self._closed:
            return
        with wx.FileDialog(
            self,
            "选择当前工程",
            wildcard="KiCad 工程 (*.kicad_pro)|*.kicad_pro",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self._set_project(Path(dialog.GetPath()).resolve().parent)

    def _navigate(self, event):
        try:
            url = normalize_navigation(self.address.GetValue())
        except ValueError as error:
            self.status.SetLabel(str(error))
            self.chrome.set_import_state("error")
            return
        if self.webview:
            self.webview.LoadURL(url)

    def _on_navigating(self, event):
        url = event.GetURL()
        if url == "about:blank":
            return
        if not _is_main_frame(event):
            # Store pages use cross-origin frames for their normal verification
            # flow. These frames cannot authorize imports or change page tokens.
            try:
                parts = urlsplit(url)
                allowed = parts.scheme.lower() in ("http", "https") and bool(parts.hostname)
            except ValueError:
                allowed = False
            if not allowed:
                event.Veto()
            return
        try:
            normalize_navigation(url)
        except ValueError:
            event.Veto()
            return
        self._page_loading = True
        self._page_token = secrets.token_urlsafe(24)

    def _on_navigated(self, event):
        if _is_main_frame(event):
            self._sync_url()

    def _on_loaded(self, event):
        if not _is_main_frame(event):
            return
        if event.GetURL() == self.webview.GetCurrentURL():
            self._page_loading = False
        self._sync_url()
        self._install_page_button()

    def _on_new_window(self, event):
        event.Veto()
        try:
            url = normalize_navigation(event.GetURL())
        except ValueError:
            self.status.SetLabel("请在外部浏览器打开此链接")
            return
        wx.CallAfter(self._load_url_if_open, url)

    def _load_url_if_open(self, url):
        if not self._closed and self.webview:
            self.webview.LoadURL(url)

    def _on_web_error(self, event):
        if not _is_main_frame(event):
            return
        self._page_loading = False
        self._page_token = secrets.token_urlsafe(24)
        if not self._busy and self._last_result is None:
            self.status.SetLabel("网页加载失败，请刷新")
            if getattr(self, "chrome", None):
                self.chrome.set_import_state("error")
        try:
            host = urlsplit(event.GetURL()).hostname or "当前页面"
        except ValueError:
            host = "当前页面"
        self._append_log(f"网页加载失败：{host}")

    def _sync_url(self):
        if not self.webview or self._closed:
            return
        url = self.webview.GetCurrentURL()
        if url != self._last_url:
            self._last_url = url
            self._page_token = secrets.token_urlsafe(24)
            self.address.ChangeValue(url)
        self.back.Enable(self.webview.CanGoBack())
        self.forward.Enable(self.webview.CanGoForward())

    def _install_page_button(self):
        if self._closed or not self.webview or self._validating_click or self._script_active:
            return
        try:
            url, token = self.webview.GetCurrentURL(), self._page_token
            normalize_navigation(url)
            ok, probe = self._run_script(refresh_script(token))
            # Edge serializes the JSON.stringify result once more; some other
            # backends return its JSON text directly. Accept either form only.
            for _ in range(2):
                if not isinstance(probe, str):
                    break
                try:
                    probe = json.loads(probe)
                except ValueError:
                    break
            if (
                self._closed
                or not self.webview
                or token != self._page_token
                or self.webview.GetCurrentURL() != url
                or not ok
                or not isinstance(probe, dict)
                or type(probe.get("installed")) is not bool
            ):
                return
            value = probe.get("componentId")
            if not isinstance(value, str) or normalize_component_id(value) != value or not value:
                return
            if not probe["installed"]:
                ok, value = self._run_script(injection_script(token))
                try:
                    value = json.loads(value)
                except (ValueError, TypeError):
                    pass
            if (
                self._closed
                or not self.webview
                or token != self._page_token
                or self.webview.GetCurrentURL() != url
                or not ok
                or not isinstance(value, str)
                or not normalize_component_id(value)
            ):
                return
            self._page_loading = False
            if self._busy and not probe["installed"]:
                # Existing runtimes preserve busy state when refreshing/replacing
                # controls. Only a newly installed runtime needs it sent again.
                self._set_page_state(self._active_component_id, "importing")
        except Exception:
            logging.debug("Could not inject the import button", exc_info=True)

    def _run_script(self, script):
        """Keep native WebView evaluations serial while wx pumps window events."""
        if self._closed or not self.webview:
            return False, ""
        if self._script_active:
            raise RuntimeError("A WebView script is already running")
        self._script_active = True
        try:
            return self.webview.RunScript(script)
        finally:
            self._script_active = False
            if self._closed:
                self._pending_page_message = None
                self._pending_page_state = None
            elif not self._script_replay_scheduled and (
                self._pending_page_message is not None or self._pending_page_state is not None
            ):
                self._script_replay_scheduled = True
                wx.CallAfter(self._replay_script_work)

    def _replay_script_work(self):
        self._script_replay_scheduled = False
        if self._closed:
            self._pending_page_message = None
            self._pending_page_state = None
            return
        if self._script_active:
            # The active evaluation's finally block will schedule another replay.
            return
        message, state = self._pending_page_message, self._pending_page_state
        self._pending_page_message = None
        self._pending_page_state = None
        if state is not None:
            self._set_page_state(*state)
        if message is not None:
            self._handle_page_import(*message)

    def _set_page_state(self, component_id, state, message=""):
        if not self._closed and self.webview and component_id:
            if self._script_active:
                self._pending_page_state = (component_id, state, str(message)[:2000])
                return
            self._run_script(status_script(component_id, state, message))

    def _on_page_import(self, event):
        # wx event objects only live for their callback. Snapshot strings before
        # deferring a message received while an evaluation pumps native events.
        self._handle_page_import(event.GetMessageHandler(), event.GetString())

    def _confirm_import_target(self, component_id, token, url):
        def document_is_current():
            return (
                not self._closed
                and not self._busy
                and not self._page_loading
                and self.webview is not None
                and token == self._page_token
                and self.webview.GetCurrentURL() == url
            )

        if not document_is_current():
            return False
        ok, value = self._run_script(current_product_script(component_id))
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            pass
        # RunScript can pump navigation, close and worker callbacks.
        if not document_is_current():
            return False
        if not ok or value != component_id:
            raise ValueError("商品信息已变化，请重新点击导入。")
        return True

    def _handle_page_import(self, handler, raw):
        if self._closed or self._busy or self._validating_click:
            return
        if handler != "lcscImport" or not isinstance(raw, str) or len(raw) > 8192:
            return
        if self._script_active:
            # One click is enough: bound memory and discard duplicate envelopes.
            if self._pending_page_message is None:
                self._pending_page_message = (handler, raw)
            return
        if self._page_loading:
            return
        self._validating_click = True
        component_id = None
        try:
            token, url = self._page_token, self.webview.GetCurrentURL()
            component_id = parse_import_message(raw, url, token)
            if not self._confirm_import_target(component_id, token, url):
                return
            if not self._project:
                self._choose_project(None)
                if not self._project:
                    self._set_page_state(component_id, "idle")
                    self.status.SetLabel("请选择工程")
                    return
                # Search results can change in place while the native picker is open.
                if not self._confirm_import_target(component_id, token, url):
                    return
            self._start_import(component_id)
        except (ValueError, RuntimeError) as error:
            self._append_log(str(error))
            self._set_page_state(component_id, "error", str(error))
            if component_id:
                self.status.SetLabel(str(error))
                if getattr(self, "chrome", None):
                    self.chrome.set_import_state("error")
        finally:
            self._validating_click = False

    def _poll_page(self, event):
        if self._closed or not self.webview:
            return
        self._sync_url()
        # Edge can keep IsBusy true after a vetoed iframe navigation. Injection
        # checks the actual document's product and every click checks it again.
        self._install_page_button()

    def _append_log(self, message):
        if not self._closed:
            self.log.AppendText(str(message) + "\n")

    def _import_progress(self, component_id, message):
        # Queued worker updates must not replace the result of a completed job.
        if self._closed or not self._busy or component_id != self._active_component_id:
            return
        message = str(message)
        self._append_log(message)

    def _start_import(self, component_id):
        component_id = normalize_component_id(component_id)
        if self._busy or not component_id or not self._project:
            return
        # Snapshot both values on the GUI thread; later navigation cannot change the job.
        project = self._project
        self._busy = True
        self._active_component_id = component_id
        self._last_result = None
        self._last_error = None
        self.project_button.Disable()
        if getattr(self, "chrome", None):
            self.chrome.set_import_state("busy", component_id=component_id)
        self._set_page_state(component_id, "importing")
        self.status.SetLabel(f"正在导入 {component_id}")
        self._append_log(f"\n{component_id} → {project}")
        try:
            Thread(target=self._import_worker, args=(component_id, project), daemon=False).start()
        except Exception as error:
            logging.exception("Could not start the LCSC import worker")
            self._finish_import(component_id, None, str(error))

    def _import_worker(self, component_id, project):
        try:
            result = import_component_to_project(
                component_id,
                project,
                print_func=lambda text: wx.CallAfter(self._import_progress, component_id, text),
            )
        except Exception as error:
            logging.exception("LCSC project import failed")
            wx.CallAfter(self._finish_import, component_id, None, str(error))
        else:
            wx.CallAfter(self._finish_import, component_id, result, None)

    def _finish_import(self, component_id, result, error):
        if self._closed:
            return
        self._busy = False
        self._last_result = result
        self._last_error = error
        self.project_button.Enable()
        if getattr(self, "chrome", None):
            self.chrome.set_import_state("error" if error else "success", result=result)
        if error:
            self.status.SetLabel(f"{component_id} 导入失败")
            self._append_log(f"导入失败：{error}")
            self._set_page_state(component_id, "error", "导入失败，点击重试")
            return
        action = "已存在" if result.already_present else "已导入"
        self.status.SetLabel(f"{component_id} {action}")
        self._set_page_state(component_id, "success", action)
        self._append_log(f"{component_id} {action}\n{result.directory}")

    def _on_close(self, event):
        if self._busy:
            self.status.SetLabel("正在导入，请稍候")
            if hasattr(event, "CanVeto") and event.CanVeto():
                event.Veto()
            return
        self._closed = True
        self._timer.Stop()
        if getattr(self, "chrome", None):
            self.chrome.close()
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)
        else:
            self.Destroy()


def main():
    parser = argparse.ArgumentParser(description="立创元件桥 · LCSC KiCad Bridge")
    parser.add_argument("--project", type=Path, help="工程目录或 .kicad_pro 文件")
    parser.add_argument("--url", default=HOME_URL)
    args = parser.parse_args()
    project = args.project
    if project and project.is_file() and project.suffix == ".kicad_pro":
        project = project.parent
    if project and (not project.is_dir() or not any(project.glob("*.kicad_pro"))):
        parser.error("请选择已保存的 KiCad 工程目录或 .kicad_pro 文件")
    try:
        url = normalize_navigation(args.url)
    except ValueError as error:
        parser.error(str(error))
    app = wx.App(False)
    dialog = LCSCBrowser(project_dir=project or detect_project(), initial_url=url)
    try:
        dialog.ShowModal()
    finally:
        dialog.Destroy()
        app.Destroy()


if __name__ == "__main__":
    main()
