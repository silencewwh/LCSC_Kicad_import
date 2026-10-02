"""Regression checks for page-message/import races without creating a WebView."""

import json
import sys
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
try:
    import lcsc_browser as browser
except ImportError:
    browser = None

URL = "https://item.szlcsc.com/2392.html"
LIST_URL = "https://so.szlcsc.com/global.html?k=resistor"
TOKEN = "current-document-token"


class Text:
    def __init__(self, value=""):
        self.value = value
        self.enabled = True

    def GetValue(self):
        return self.value

    def ChangeValue(self, value):
        self.value = value

    SetLabel = ChangeValue

    def Enable(self, enabled=True):
        self.enabled = enabled

    def Disable(self):
        self.enabled = False


@unittest.skipIf(browser is None, "KiCad wxPython is required")
class BrowserStateTests(unittest.TestCase):
    def script_result(self, code, installed=True, component_id="C2040"):
        if "return JSON.stringify({installed:" in code:
            return True, json.dumps(
                json.dumps({"installed": installed, "componentId": component_id})
            )
        return True, json.dumps(component_id)

    def navigation_event(self, url=URL, main=True, target="", legacy=False):
        event = SimpleNamespace(
            GetURL=lambda: url,
            GetTarget=lambda: target,
            GetString=lambda: "load error",
            Veto=Mock(),
        )
        if not legacy:
            event.IsTargetMainFrame = lambda: main
        return event

    def make_state(self):
        state = SimpleNamespace(
            _closed=False,
            _busy=False,
            _page_loading=False,
            _page_token=TOKEN,
            _script_active=False,
            _script_replay_scheduled=False,
            _pending_page_message=None,
            _pending_page_state=None,
            _validating_click=False,
            _active_component_id=None,
            _last_result=None,
            _last_error=None,
            _last_url=URL,
            _project=Path("project"),
            address=Text(URL),
            back=Text(),
            forward=Text(),
            project_button=Text(),
            status=Text(),
            webview=SimpleNamespace(
                GetCurrentURL=Mock(return_value=URL),
                CanGoBack=lambda: False,
                CanGoForward=lambda: False,
                IsBusy=Mock(return_value=False),
                RunScript=Mock(side_effect=self.script_result),
            ),
            _append_log=Mock(),
            _set_page_state=Mock(),
            _choose_project=Mock(),
            _import_worker=Mock(),
            _timer=SimpleNamespace(Stop=Mock()),
            IsModal=lambda: True,
            EndModal=Mock(),
            Destroy=Mock(),
        )
        for name in (
            "_start_import",
            "_on_page_import",
            "_confirm_import_target",
            "_handle_page_import",
            "_sync_url",
            "_install_page_button",
            "_run_script",
            "_replay_script_work",
        ):
            setattr(state, name, MethodType(getattr(browser.LCSCBrowser, name), state))
        return state

    def message(self, **changes):
        payload = dict(version=1, type="import", id="C2040", url=URL, token=TOKEN)
        payload.update(changes)
        return SimpleNamespace(
            GetMessageHandler=lambda: "lcscImport", GetString=lambda: json.dumps(payload)
        )

    def test_page_load_and_poll_only_inject_without_importing(self):
        state = self.make_state()
        with patch.object(browser, "Thread") as thread:
            browser.LCSCBrowser._poll_page(state, None)
            state.webview.RunScript.assert_called_once()
            self.assertEqual(
                browser.refresh_script(TOKEN), state.webview.RunScript.call_args.args[0]
            )
            thread.assert_not_called()
            self.assertFalse(state._busy)

    def test_full_injection_only_follows_confirmed_product_without_runtime(self):
        state = self.make_state()
        state.webview.RunScript.side_effect = lambda code: self.script_result(code, installed=False)
        state._install_page_button()
        self.assertEqual(
            [call.args[0] for call in state.webview.RunScript.call_args_list],
            [browser.refresh_script(TOKEN), browser.injection_script(TOKEN)],
        )
        state.webview.RunScript.reset_mock()
        state.webview.RunScript.side_effect = self.script_result
        state._install_page_button()
        state._install_page_button()
        self.assertEqual(state.webview.RunScript.call_count, 2)
        self.assertTrue(
            all(
                call.args[0] == browser.refresh_script(TOKEN)
                for call in state.webview.RunScript.call_args_list
            )
        )

    def test_busy_poll_refreshes_existing_runtime_without_resending_status(self):
        state = self.make_state()
        state._busy = True
        state._active_component_id = "C2040"
        state._install_page_button()
        state._install_page_button()
        self.assertEqual(state.webview.RunScript.call_count, 2)
        state._set_page_state.assert_not_called()

    def test_busy_poll_restores_state_when_a_new_page_runtime_is_installed(self):
        state = self.make_state()
        state._busy = True
        state._active_component_id = "C2040"
        state.webview.RunScript.side_effect = lambda code: self.script_result(code, installed=False)
        state._install_page_button()
        self.assertEqual(state.webview.RunScript.call_count, 2)
        state._set_page_state.assert_called_once_with("C2040", "importing")

    def test_unconfirmed_page_never_receives_full_injection(self):
        for probe in (
            {"installed": False, "componentId": ""},
            {"installed": False, "componentId": "C0"},
            {"installed": False, "componentId": " C2040 "},
            {"installed": False, "componentId": 2040},
            {"installed": "false", "componentId": "C2040"},
            {"componentId": "C2040"},
            [],
            None,
        ):
            with self.subTest(probe=probe):
                state = self.make_state()
                state._page_loading = True
                state.webview.RunScript.side_effect = lambda code: (True, json.dumps(probe))
                state._install_page_button()
                state.webview.RunScript.assert_called_once()
                self.assertTrue(state._page_loading)

    def test_probe_supports_direct_json_result(self):
        state = self.make_state()
        state._page_loading = True
        state.webview.RunScript.side_effect = lambda code: (
            True,
            json.dumps({"installed": True, "componentId": "C2040"}),
        )
        state._install_page_button()
        state.webview.RunScript.assert_called_once()
        self.assertFalse(state._page_loading)

    def test_probe_or_injection_race_never_marks_changed_document_ready(self):
        for stage in (1, 2):
            for mutation in ("token", "url", "closed"):
                with self.subTest(stage=stage, mutation=mutation):
                    state = self.make_state()
                    state._page_loading = True
                    state._busy = True
                    state._active_component_id = "C2040"

                    def script(code):
                        if state.webview.RunScript.call_count == stage:
                            if mutation == "token":
                                state._page_token = "changed"
                            elif mutation == "url":
                                state.webview.GetCurrentURL.return_value = (
                                    "https://item.szlcsc.com/2403.html"
                                )
                            else:
                                state._closed = True
                        return self.script_result(code, installed=False)

                    state.webview.RunScript.side_effect = script
                    state._install_page_button()
                    self.assertEqual(state.webview.RunScript.call_count, stage)
                    self.assertTrue(state._page_loading)
                    state._set_page_state.assert_not_called()

    def test_validating_or_closed_webview_does_not_inject(self):
        for condition in ("validating", "closed", "script_active"):
            state = self.make_state()
            if condition == "validating":
                state._validating_click = True
            elif condition == "closed":
                state._closed = True
            else:
                state._script_active = True
            state._install_page_button()
            state.webview.RunScript.assert_not_called()

    def test_sticky_native_busy_flag_does_not_suppress_ready_main_document(self):
        state = self.make_state()
        state._page_loading = True
        state.webview.IsBusy.return_value = True
        state._install_page_button()
        state.webview.RunScript.assert_called_once()
        self.assertFalse(state._page_loading)

    def test_stale_injection_cannot_mark_new_document_ready(self):
        state = self.make_state()
        state._page_loading = True

        def script(code):
            state._page_token = "changed-document"
            return self.script_result(code)

        state.webview.RunScript.side_effect = script
        state._install_page_button()
        self.assertTrue(state._page_loading)

    def test_navigation_invalidates_previous_document_token(self):
        state = self.make_state()
        state.webview.GetCurrentURL.return_value = "https://item.szlcsc.com/2403.html"
        state._sync_url()
        self.assertNotEqual(state._page_token, TOKEN)
        self.assertEqual(state.address.GetValue(), "https://item.szlcsc.com/2403.html")

    def test_cross_origin_web_frames_do_not_change_main_document_state(self):
        for url in (
            "https://verification.example/frame?challenge=private",
            "http://verification.example/frame",
            "about:blank",
        ):
            with self.subTest(url=url):
                state = self.make_state()
                event = self.navigation_event(url=url, main=False, target="")
                browser.LCSCBrowser._on_navigating(state, event)
                event.Veto.assert_not_called()
                self.assertEqual(state._page_token, TOKEN)
                self.assertFalse(state._page_loading)

    def test_subframes_cannot_navigate_to_non_web_schemes(self):
        for url in (
            "file:///C:/private.txt",
            "javascript:alert(1)",
            "data:text/html,hello",
            "custom-app://open",
            "about:config",
            "https:///missing-host",
        ):
            with self.subTest(url=url):
                state = self.make_state()
                event = self.navigation_event(url=url, main=False)
                browser.LCSCBrowser._on_navigating(state, event)
                event.Veto.assert_called_once()
                self.assertEqual(state._page_token, TOKEN)

    def test_main_frame_still_requires_store_url_even_with_named_target(self):
        state = self.make_state()
        blocked = self.navigation_event(
            url="https://verification.example/?secret=private", main=True, target="named-frame"
        )
        browser.LCSCBrowser._on_navigating(state, blocked)
        blocked.Veto.assert_called_once()
        self.assertEqual(state._page_token, TOKEN)
        allowed = self.navigation_event(main=True, target="named-frame")
        browser.LCSCBrowser._on_navigating(state, allowed)
        allowed.Veto.assert_not_called()
        self.assertNotEqual(state._page_token, TOKEN)
        self.assertTrue(state._page_loading)

    def test_frame_load_and_error_do_not_inject_or_overwrite_import_state(self):
        state = self.make_state()
        state._page_loading = True
        state.status.SetLabel("C2040 已导入")
        state._sync_url = Mock()
        state._install_page_button = Mock()
        event = self.navigation_event(main=False, target="")
        browser.LCSCBrowser._on_navigated(state, event)
        browser.LCSCBrowser._on_loaded(state, event)
        browser.LCSCBrowser._on_web_error(state, event)
        state._sync_url.assert_not_called()
        state._install_page_button.assert_not_called()
        state._append_log.assert_not_called()
        self.assertEqual(state._page_token, TOKEN)
        self.assertTrue(state._page_loading)
        self.assertEqual(state.status.GetValue(), "C2040 已导入")

    def test_main_load_injects_and_main_error_logs_only_host(self):
        state = self.make_state()
        state._page_loading = True
        state._install_page_button = Mock()
        browser.LCSCBrowser._on_loaded(state, self.navigation_event(target="named-frame"))
        state._install_page_button.assert_called_once()
        self.assertFalse(state._page_loading)
        event = self.navigation_event(url=URL + "?token=private", target="named-frame")
        browser.LCSCBrowser._on_web_error(state, event)
        self.assertNotEqual(state._page_token, TOKEN)
        self.assertIn("网页加载失败", state.status.GetValue())
        self.assertEqual(state._append_log.call_args.args, ("网页加载失败：item.szlcsc.com",))

    def test_legacy_backend_uses_target_name_fallback(self):
        for target, main in (("", True), ("_self", True), ("verification-frame", False)):
            with self.subTest(target=target):
                state = self.make_state()
                event = self.navigation_event(target=target, legacy=True)
                browser.LCSCBrowser._on_navigating(state, event)
                self.assertEqual(state._page_loading, main)
                self.assertEqual(state._page_token == TOKEN, not main)

    def test_malformed_and_stale_messages_never_start_import(self):
        invalid = [
            self.message(token="old-document"),
            self.message(url="https://item.szlcsc.com/2403.html"),
            self.message(id="C0"),
            self.message(path="D:/elsewhere"),
            SimpleNamespace(GetMessageHandler=lambda: "lcscImport", GetString=lambda: "invalid"),
            SimpleNamespace(GetMessageHandler=lambda: "other", GetString=lambda: "invalid"),
        ]
        for event in invalid:
            state = self.make_state()
            with patch.object(browser, "Thread") as thread:
                state._on_page_import(event)
                thread.assert_not_called()
                state.webview.RunScript.assert_not_called()
                self.assertFalse(state._validating_click)

    def test_main_product_mismatch_is_rejected(self):
        state = self.make_state()
        state.webview.RunScript.side_effect = lambda code: (True, '"C2051"')
        with patch.object(browser, "Thread") as thread:
            state._on_page_import(self.message())
            thread.assert_not_called()
        self.assertEqual(state._set_page_state.call_args.args[:2], ("C2040", "error"))
        self.assertFalse(state._validating_click)

    def test_list_click_validates_second_row_instead_of_first_injection_signal(self):
        state = self.make_state()
        state.webview.GetCurrentURL.return_value = LIST_URL
        state.webview.RunScript.side_effect = [
            (True, json.dumps({"installed": True, "componentId": "C2040"})),
            (True, json.dumps("C2051")),
        ]
        with (
            patch.object(browser, "Thread") as thread,
            patch.object(
                browser, "current_product_script", wraps=browser.current_product_script
            ) as selector,
        ):
            state._install_page_button()
            thread.assert_not_called()
            state._on_page_import(self.message(id="C2051", url=LIST_URL))
            selector.assert_called_once_with("C2051")
            self.assertEqual(thread.call_args.kwargs["args"], ("C2051", Path("project")))
        self.assertEqual(state.webview.RunScript.call_count, 2)
        self.assertEqual(state._active_component_id, "C2051")

    def test_list_target_is_rechecked_after_picker_even_when_url_and_token_match(self):
        for changed_result in ("", "C2052"):
            with self.subTest(changed_result=changed_result):
                state = self.make_state()
                state._project = None
                state.webview.GetCurrentURL.return_value = LIST_URL
                state.webview.RunScript.side_effect = [
                    (True, json.dumps("C2051")),
                    (True, json.dumps(changed_result)),
                ]

                def choose(event):
                    state._project = Path("selected-project")

                state._choose_project.side_effect = choose
                with patch.object(browser, "Thread") as thread:
                    state._on_page_import(self.message(id="C2051", url=LIST_URL))
                    thread.assert_not_called()
                self.assertEqual(state.webview.RunScript.call_count, 2)
                self.assertEqual(state._page_token, TOKEN)
                self.assertEqual(state.webview.GetCurrentURL(), LIST_URL)
                self.assertFalse(state._busy)
                self.assertFalse(state._validating_click)
                self.assertEqual(state._set_page_state.call_args.args[:2], ("C2051", "error"))

    def test_list_click_still_rejects_busy_or_navigation_before_dom_validation(self):
        for condition in ("busy", "loading", "token", "url"):
            with self.subTest(condition=condition):
                state = self.make_state()
                state.webview.GetCurrentURL.return_value = LIST_URL
                if condition == "busy":
                    state._busy = True
                elif condition == "loading":
                    state._page_loading = True
                elif condition == "token":
                    state._page_token = "another-document"
                else:
                    state.webview.GetCurrentURL.return_value = LIST_URL + "&page=2"
                with patch.object(browser, "Thread") as thread:
                    state._on_page_import(self.message(id="C2051", url=LIST_URL))
                    thread.assert_not_called()
                state.webview.RunScript.assert_not_called()
                state._choose_project.assert_not_called()

    def test_picker_navigation_or_busy_transition_blocks_list_revalidation(self):
        for condition in ("busy", "loading", "token", "url", "closed"):
            with self.subTest(condition=condition):
                state = self.make_state()
                state._project = None
                state.webview.GetCurrentURL.return_value = LIST_URL
                state.webview.RunScript.side_effect = lambda code: (True, json.dumps("C2051"))

                def choose(event):
                    state._project = Path("selected-project")
                    if condition == "busy":
                        state._busy = True
                    elif condition == "loading":
                        state._page_loading = True
                    elif condition == "token":
                        state._page_token = "another-document"
                    elif condition == "url":
                        state.webview.GetCurrentURL.return_value = LIST_URL + "&page=2"
                    else:
                        state._closed = True

                state._choose_project.side_effect = choose
                with patch.object(browser, "Thread") as thread:
                    state._on_page_import(self.message(id="C2051", url=LIST_URL))
                    thread.assert_not_called()
                state.webview.RunScript.assert_called_once()

    def test_valid_click_captures_product_and_project_and_rejects_duplicate(self):
        state = self.make_state()
        with patch.object(browser, "Thread") as thread:
            state._on_page_import(self.message())
            state._project = Path("other-project")
            self.assertEqual(thread.call_args.kwargs["args"], ("C2040", Path("project")))
            self.assertTrue(state._busy)
            self.assertFalse(state.project_button.enabled)
            state._on_page_import(self.message())
            self.assertEqual(thread.call_count, 1)

    def test_script_reentrancy_cannot_start_second_import(self):
        state = self.make_state()

        def script(code):
            state._on_page_import(self.message())
            return self.script_result(code)

        state.webview.RunScript.side_effect = script
        with patch.object(browser, "Thread") as thread:
            state._on_page_import(self.message())
            self.assertEqual(thread.call_count, 1)
        self.assertEqual(state.webview.RunScript.call_count, 1)
        self.assertFalse(state._validating_click)

    def test_click_during_injection_replays_once_after_native_script_returns(self):
        state = self.make_state()
        callbacks = []
        nesting = []

        def script(code):
            nesting.append(state.webview.RunScript.call_count)
            if len(nesting) == 2:
                state._on_page_import(self.message())
                state._on_page_import(self.message())
                self.assertFalse(state._busy)
                self.assertEqual(state.webview.RunScript.call_count, 2)
            return self.script_result(code, installed=False)

        state.webview.RunScript.side_effect = script
        with patch.object(browser.wx, "CallAfter", side_effect=lambda fn: callbacks.append(fn)):
            with patch.object(browser, "Thread") as thread:
                state._install_page_button()
                thread.assert_not_called()
                self.assertEqual(len(callbacks), 1)
                self.assertFalse(state._script_active)
                callbacks.pop()()
                self.assertEqual(thread.call_count, 1)
                self.assertEqual(state.webview.RunScript.call_count, 3)
                self.assertIsNone(state._pending_page_message)

    def test_queued_click_is_revalidated_after_document_changes(self):
        state = self.make_state()
        callbacks = []

        def script(code):
            state._on_page_import(self.message())
            return self.script_result(code)

        state.webview.RunScript.side_effect = script
        with patch.object(browser.wx, "CallAfter", side_effect=lambda fn: callbacks.append(fn)):
            with patch.object(browser, "Thread") as thread:
                state._install_page_button()
                state._page_token = "new-document"
                callbacks.pop()()
                thread.assert_not_called()
                self.assertEqual(state.webview.RunScript.call_count, 1)
                self.assertIsNone(state._pending_page_message)

    def test_queued_message_is_bounded_and_dropped_on_close(self):
        state = self.make_state()
        callbacks = []

        def script(code):
            state._handle_page_import("lcscImport", "x" * 8193)
            self.assertIsNone(state._pending_page_message)
            state._on_page_import(self.message())
            return self.script_result(code)

        state.webview.RunScript.side_effect = script
        with patch.object(browser.wx, "CallAfter", side_effect=lambda fn: callbacks.append(fn)):
            with patch.object(browser, "Thread") as thread:
                state._install_page_button()
                state._closed = True
                callbacks.pop()()
                thread.assert_not_called()
                self.assertIsNone(state._pending_page_message)
                self.assertFalse(state._script_replay_scheduled)

    def test_worker_status_during_evaluation_is_deferred_without_nested_script(self):
        state = self.make_state()
        state._set_page_state = MethodType(browser.LCSCBrowser._set_page_state, state)
        callbacks = []

        def script(code):
            if state.webview.RunScript.call_count == 1:
                state._set_page_state("C2040", "success", "已导入")
                self.assertEqual(state.webview.RunScript.call_count, 1)
            return self.script_result(code)

        state.webview.RunScript.side_effect = script
        with patch.object(browser.wx, "CallAfter", side_effect=lambda fn: callbacks.append(fn)):
            state._install_page_button()
            self.assertEqual(state.webview.RunScript.call_count, 1)
            callbacks.pop()()
            self.assertEqual(state.webview.RunScript.call_count, 2)
            self.assertIsNone(state._pending_page_state)

    def test_navigation_busy_or_close_during_script_execution_cancels_click(self):
        for mutation in ("token", "url", "closed", "busy", "loading"):
            state = self.make_state()

            def script(code):
                if mutation == "token":
                    state._page_token = "changed"
                elif mutation == "url":
                    state.webview.GetCurrentURL.return_value = "https://item.szlcsc.com/2403.html"
                elif mutation == "busy":
                    state._busy = True
                elif mutation == "loading":
                    state._page_loading = True
                else:
                    state._closed = True
                return True, '"C2040"'

            state.webview.RunScript.side_effect = script
            with patch.object(browser, "Thread") as thread:
                state._on_page_import(self.message())
                thread.assert_not_called()

    def test_cancelled_project_picker_restores_button(self):
        state = self.make_state()
        state._project = None
        with patch.object(browser, "Thread") as thread:
            state._on_page_import(self.message())
            thread.assert_not_called()
        state._choose_project.assert_called_once_with(None)
        state._set_page_state.assert_called_with("C2040", "idle")

    def test_details_toggle_updates_native_visibility_and_shared_model(self):
        state = self.make_state()
        state.chrome = SimpleNamespace(model={"panelOpen": False})
        state.chrome.set = lambda key, value: state.chrome.model.update({key: value})
        state.details_panel = SimpleNamespace(Show=Mock())
        state.Layout = Mock()
        browser.LCSCBrowser._toggle_details(state)
        self.assertTrue(state.chrome.model["panelOpen"])
        state.details_panel.Show.assert_called_with(True)
        browser.LCSCBrowser._toggle_details(state)
        self.assertFalse(state.chrome.model["panelOpen"])
        state.details_panel.Show.assert_called_with(False)
        self.assertEqual(state.Layout.call_count, 2)
        state._closed = True
        browser.LCSCBrowser._toggle_details(state)
        self.assertEqual(state.Layout.call_count, 2)

    def test_progress_keeps_concise_status_and_detailed_log(self):
        state = self.make_state()
        state._busy = True
        state._active_component_id = "C2040"
        state.status.SetLabel("正在导入 C2040")
        detail = "正在下载 C2040 的符号与封装数据"
        browser.LCSCBrowser._import_progress(state, "C2040", detail)
        self.assertEqual(state.status.GetValue(), "正在导入 C2040")
        state._append_log.assert_called_once_with(detail)

    def test_failed_import_keeps_error_detail_and_restores_project_selection(self):
        state = self.make_state()
        state._busy = True
        state.chrome = SimpleNamespace(set_import_state=Mock())
        detail = "模型文件校验失败：invalid.step"
        browser.LCSCBrowser._finish_import(state, "C2040", None, detail)
        self.assertEqual(state.status.GetValue(), "C2040 导入失败")
        self.assertEqual(state._last_error, detail)
        self.assertTrue(state.project_button.enabled)
        self.assertFalse(state._busy)
        state.chrome.set_import_state.assert_called_once_with("error", result=None)
        state._append_log.assert_called_once_with("导入失败：" + detail)

    def test_navigation_during_project_picker_cancels_click(self):
        state = self.make_state()
        state._project = None

        def choose(event):
            state._project = Path("selected-project")
            state._page_token = "changed-while-picker-open"

        state._choose_project.side_effect = choose
        with patch.object(browser, "Thread") as thread:
            state._on_page_import(self.message())
            thread.assert_not_called()

    def test_project_picker_selection_is_used_for_same_click(self):
        state = self.make_state()
        state._project = None
        state.project_text = Mock()
        state.chrome = SimpleNamespace(set_import_state=Mock())

        def choose(event):
            browser.LCSCBrowser._set_project(state, Path("selected-project"))

        state._choose_project.side_effect = choose
        with patch.object(browser, "Thread") as thread:
            state._on_page_import(self.message())
            self.assertEqual(thread.call_args.kwargs["args"], ("C2040", Path("selected-project")))
        self.assertEqual(state.webview.RunScript.call_count, 2)
        self.assertEqual(state._page_token, TOKEN)

    def test_project_change_clears_old_result_and_reinstalls_page_controls(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                state = self.make_state()
                state.project_text = Mock()
                state.chrome = SimpleNamespace(set_import_state=Mock())
                state._last_result = None if failed else object()
                state._last_error = "old project error" if failed else None
                state._active_component_id = "C2040"
                state.status.SetLabel("C2040 导入失败" if failed else "C2040 已导入")
                state._pending_page_message = ("lcscImport", "old-project-click")
                state._pending_page_state = ("C2040", "success", "已导入")
                state.webview.RunScript.side_effect = lambda code: self.script_result(
                    code, installed=False
                )

                browser.LCSCBrowser._set_project(state, Path("new-project"))

                self.assertEqual(state._project, Path("new-project"))
                self.assertIsNone(state._last_result)
                self.assertIsNone(state._last_error)
                self.assertIsNone(state._active_component_id)
                self.assertEqual(state.status.GetValue(), "")
                self.assertIsNone(state._pending_page_message)
                self.assertIsNone(state._pending_page_state)
                self.assertNotEqual(state._page_token, TOKEN)
                state.chrome.set_import_state.assert_called_once_with("idle")
                self.assertEqual(
                    [call.args[0] for call in state.webview.RunScript.call_args_list],
                    [
                        browser.refresh_script(state._page_token),
                        browser.injection_script(state._page_token),
                    ],
                )
                with patch.object(browser, "Thread") as thread:
                    state._on_page_import(self.message())
                    thread.assert_not_called()

    def test_same_project_selection_preserves_current_result_and_page_token(self):
        state = self.make_state()
        state.project_text = Mock()
        state.chrome = SimpleNamespace(set_import_state=Mock())
        result = state._last_result = object()
        state.status.SetLabel("C2040 已导入")

        browser.LCSCBrowser._set_project(state, state._project)

        self.assertIs(state._last_result, result)
        self.assertEqual(state.status.GetValue(), "C2040 已导入")
        self.assertEqual(state._page_token, TOKEN)
        state.chrome.set_import_state.assert_not_called()
        state.webview.RunScript.assert_not_called()

    def test_close_is_vetoed_during_import_then_allowed_after_completion(self):
        state = self.make_state()
        state._busy = True
        event = SimpleNamespace(CanVeto=lambda: True, Veto=Mock())
        browser.LCSCBrowser._on_close(state, event)
        event.Veto.assert_called_once()
        self.assertFalse(state._closed)
        state._timer.Stop.assert_not_called()
        state._busy = False
        browser.LCSCBrowser._on_close(state, event)
        self.assertTrue(state._closed)
        state._timer.Stop.assert_called_once()
        state.EndModal.assert_called_once()


if __name__ == "__main__":
    unittest.main()
