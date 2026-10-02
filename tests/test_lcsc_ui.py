"""Exercise shell trust, project selection and cross-WebView event lifecycles."""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
try:
    import lcsc_ui as ui
except ImportError:
    ui = None


def envelope(action, **fields):
    return json.dumps({"version": 1, "action": action, **fields})


def event(raw, handler="lcscShell"):
    return SimpleNamespace(GetMessageHandler=lambda: handler, GetString=lambda: raw)


@unittest.skipIf(ui is None, "KiCad wxPython is required")
class ShellProtocolTests(unittest.TestCase):
    def test_supported_commands_do_not_include_import_or_file_access(self):
        for action in (
            "ready",
            "back",
            "forward",
            "home",
            "reload",
            "choose-project",
            "toggle-panel",
        ):
            self.assertEqual(ui.parse_shell_message(envelope(action)), (action, ""))
        self.assertEqual(
            ui.parse_shell_message(envelope("navigate", value="C2040")), ("navigate", "C2040")
        )
        for action in ("import", "execute", "open-file", "delete", "choose_project"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                ui.parse_shell_message(envelope(action))

    def test_malformed_or_extended_envelopes_are_rejected(self):
        cases = [
            None,
            {},
            "[]",
            "null",
            "{broken",
            "[" * 1500 + "]" * 1500,
            "x" * 8193,
            envelope("navigate"),
            envelope("navigate", value=None),
            envelope("navigate", value=["C2040"]),
            envelope("navigate", value="x" * 4097),
            envelope("choose-project", path="C:/wrong-project"),
            envelope("toggle-panel", value=True),
            envelope("reload", value="ignored"),
            json.dumps({"version": True, "action": "ready"}),
            json.dumps({"version": 1.0, "action": "ready"}),
            json.dumps({"version": 2, "action": "ready"}),
            json.dumps({"action": "ready"}),
            json.dumps({"version": 1, "action": []}),
        ]
        for raw in cases:
            with self.subTest(raw=str(raw)[:80]), self.assertRaises(ValueError):
                ui.parse_shell_message(raw)


@unittest.skipIf(ui is None, "KiCad wxPython is required")
class ShellLifecycleTests(unittest.TestCase):
    def make_chrome(self):
        chrome = ui.BrowserChrome.__new__(ui.BrowserChrome)
        chrome.owner = SimpleNamespace(
            _closed=False,
            _busy=False,
            _script_active=False,
            _pending_page_message=None,
            _pending_page_state=None,
            _script_replay_scheduled=False,
            _replay_script_work=Mock(),
            _choose_project=Mock(),
            _toggle_details=Mock(),
            _navigate=Mock(),
            address=Mock(),
            webview=Mock(),
        )
        chrome.model = {
            "projectPath": "",
            "projectName": "",
            "address": "",
            "busy": False,
            "panelOpen": False,
            "status": "Ready",
            "logs": [],
        }
        chrome.views = [Mock(name="sidebar"), Mock(name="toolbar")]
        chrome.ready = set()
        chrome.dirty = True
        chrome.evaluating = False
        chrome.flushing = False
        chrome.pending_actions = []
        chrome.closed = False
        chrome.timer = SimpleNamespace(Stop=Mock())
        chrome._urls = {}
        for name, view in zip(("sidebar", "toolbar"), chrome.views):
            url = "file:///C:/bridge/ui/index.html#" + name
            chrome._urls[view] = url
            view.GetCurrentURL.return_value = url
            view.RunScript.return_value = (True, "true")
        return chrome

    def queue(self, chrome, action, **fields):
        chrome.on_message(chrome.views[0], event(envelope(action, **fields)))

    def test_only_trusted_local_view_and_handler_can_queue_actions(self):
        chrome = self.make_chrome()
        view = chrome.views[0]
        foreign_view = Mock()
        foreign_view.GetCurrentURL.return_value = chrome._urls[view]
        for source, message in (
            (view, event(envelope("choose-project"), "lcscImport")),
            (view, event(envelope("import"))),
            (view, event(envelope("choose-project", path="C:/wrong"))),
            (foreign_view, event(envelope("choose-project"))),
        ):
            chrome.on_message(source, message)
        for url in (
            "https://item.szlcsc.com/2392.html",
            "file:///C:/bridge/ui/index.html#unknown",
            "file:///C:/another/index.html#sidebar",
            "about:blank",
        ):
            view.GetCurrentURL.return_value = url
            chrome.on_message(view, event(envelope("choose-project")))
        chrome.flush()
        self.assertEqual(chrome.pending_actions, [])
        self.assertEqual(chrome.ready, set())
        chrome.owner._choose_project.assert_not_called()
        chrome.owner._navigate.assert_not_called()

    def test_queued_action_is_revalidated_after_local_view_navigates(self):
        chrome = self.make_chrome()
        self.queue(chrome, "choose-project")
        chrome.views[0].GetCurrentURL.return_value = "https://example.org/"
        chrome.flush()
        chrome.owner._choose_project.assert_not_called()
        self.assertEqual(chrome.pending_actions, [])

    def test_first_ready_receives_latest_state_and_late_second_view_gets_full_state(self):
        chrome = self.make_chrome()
        chrome.set("status", "Current state before either HTML document is ready")
        chrome.flush()
        for view in chrome.views:
            view.RunScript.assert_not_called()
        first, second = chrome.views
        chrome.on_message(first, event(envelope("ready")))
        chrome.flush()
        self.assertIn(chrome.model["status"], first.RunScript.call_args.args[0])
        second.RunScript.assert_not_called()
        chrome.set("status", "New state before second view is ready")
        chrome.on_message(second, event(envelope("ready")))
        chrome.flush()
        for view in chrome.views:
            self.assertIn(chrome.model["status"], view.RunScript.call_args.args[0])

    def test_busy_project_selection_is_rechecked_when_dispatching(self):
        chrome = self.make_chrome()
        self.queue(chrome, "choose-project")
        # An import can start after the shell click was received but before dispatch.
        chrome.owner._busy = True
        chrome.flush()
        chrome.owner._choose_project.assert_not_called()
        chrome.owner._busy = False
        self.queue(chrome, "choose-project")
        chrome.flush()
        chrome.owner._choose_project.assert_called_once_with(None)

    def test_details_remain_accessible_during_import(self):
        chrome = self.make_chrome()
        chrome.owner._busy = True
        self.queue(chrome, "toggle-panel")
        chrome.flush()
        chrome.owner._toggle_details.assert_called_once_with()
        chrome.owner._choose_project.assert_not_called()

    def test_error_opens_details_only_when_collapsed(self):
        chrome = self.make_chrome()
        chrome.set_import_state("error")
        chrome.owner._toggle_details.assert_called_once_with()
        chrome.model["panelOpen"] = True
        chrome.owner._toggle_details.reset_mock()
        chrome.set_import_state("error")
        chrome.owner._toggle_details.assert_not_called()

    def test_idle_clears_the_previous_project_result(self):
        chrome = self.make_chrome()
        chrome.model.update(
            statusTone="success", lastResult={"componentId": "C2040", "libraryName": "LCSC"}
        )
        chrome.set_import_state("idle")
        self.assertEqual(chrome.model["statusTone"], "idle")
        self.assertIsNone(chrome.model["lastResult"])

    def test_remote_script_blocks_both_shell_render_and_project_dialog(self):
        chrome = self.make_chrome()
        chrome.ready.update(chrome.views)
        self.queue(chrome, "choose-project")
        chrome.owner._script_active = True
        chrome.flush()
        for view in chrome.views:
            view.RunScript.assert_not_called()
        chrome.owner._choose_project.assert_not_called()
        chrome.owner._script_active = False
        chrome.flush()
        chrome.owner._choose_project.assert_called_once_with(None)
        for view in chrome.views:
            view.RunScript.assert_called_once()

    def test_update_during_native_evaluation_is_rendered_on_next_flush_without_recursion(self):
        chrome = self.make_chrome()
        view = chrome.views[0]
        chrome.ready.add(view)

        def run_script(script):
            self.assertTrue(chrome.owner._script_active)
            chrome.set("status", "State changed while native events were pumped")
            chrome.flush()
            self.assertEqual(view.RunScript.call_count, 1)
            return True, "true"

        view.RunScript.side_effect = run_script
        chrome.flush()
        self.assertTrue(chrome.dirty)
        self.assertFalse(chrome.owner._script_active)
        view.RunScript.side_effect = None
        chrome.flush()
        self.assertEqual(view.RunScript.call_count, 2)
        self.assertIn(chrome.model["status"], view.RunScript.call_args.args[0])

    def test_failed_render_retries_latest_state_on_next_flush_only(self):
        for failure in ((False, ""), RuntimeError("temporary native failure")):
            with self.subTest(failure=failure):
                chrome = self.make_chrome()
                view = chrome.views[0]
                chrome.ready.add(view)
                view.RunScript.side_effect = [failure, (True, "true")]
                with patch.object(ui.wx, "CallAfter") as call_after:
                    chrome.flush()
                    self.assertTrue(chrome.dirty)
                    self.assertIn(view, chrome.ready)
                    self.assertEqual(view.RunScript.call_count, 1)
                    self.assertFalse(chrome.owner._script_active)
                    call_after.assert_not_called()

                    chrome.set("status", "Latest result")
                    chrome.flush()
                    self.assertEqual(view.RunScript.call_count, 2)
                    self.assertIn("Latest result", view.RunScript.call_args.args[0])
                    self.assertFalse(chrome.dirty)
                    chrome.flush()
                    self.assertEqual(view.RunScript.call_count, 2)
                    chrome.views[1].RunScript.assert_not_called()

    def test_failed_view_does_not_block_other_view_and_close_stops_retries(self):
        chrome = self.make_chrome()
        chrome.ready.update(chrome.views)
        chrome.views[0].RunScript.side_effect = RuntimeError("native failure")
        chrome.flush()
        for view in chrome.views:
            view.RunScript.assert_called_once()
        self.assertTrue(chrome.dirty)
        chrome.close()
        chrome.flush()
        for view in chrome.views:
            view.RunScript.assert_called_once()
        self.assertEqual(chrome.ready, set())
        chrome.timer.Stop.assert_called_once()

    def test_remote_click_pumped_by_shell_evaluation_is_replayed_after_native_return(self):
        chrome = self.make_chrome()
        chrome.ready.update(chrome.views)
        callbacks = []

        def run_script(script):
            self.assertTrue(chrome.owner._script_active)
            chrome.owner._pending_page_message = ("lcscImport", "pending-click")
            chrome.owner._pending_page_state = ("C2040", "success", "Imported")
            chrome.flush()
            return True, "true"

        for view in chrome.views:
            view.RunScript.side_effect = run_script
        with patch.object(ui.wx, "CallAfter", side_effect=lambda fn: callbacks.append(fn)):
            chrome.flush()
        self.assertFalse(chrome.owner._script_active)
        self.assertEqual(len(callbacks), 1)
        chrome.owner._replay_script_work.assert_not_called()
        callbacks[0]()
        chrome.owner._replay_script_work.assert_called_once()
        for view in chrome.views:
            view.RunScript.assert_called_once()

    def test_close_during_evaluation_stops_remaining_views_and_queued_actions(self):
        chrome = self.make_chrome()
        chrome.ready.update(chrome.views)

        def run_script(script):
            chrome.close()
            self.queue(chrome, "choose-project")
            return True, "true"

        for view in chrome.views:
            view.RunScript.side_effect = run_script
        chrome.flush()
        self.assertEqual(sum(view.RunScript.call_count for view in chrome.views), 1)
        self.assertFalse(chrome.owner._script_active)
        self.assertEqual(chrome.pending_actions, [])
        self.assertEqual(chrome.ready, set())
        chrome.timer.Stop.assert_called_once()
        chrome.flush()
        chrome.owner._choose_project.assert_not_called()

    def test_close_discards_existing_actions_and_blocks_late_ready(self):
        chrome = self.make_chrome()
        self.queue(chrome, "choose-project")
        self.queue(chrome, "navigate", value="C2040")
        chrome.close()
        self.queue(chrome, "ready")
        chrome.flush()
        self.assertEqual(chrome.pending_actions, [])
        self.assertEqual(chrome.ready, set())
        chrome.owner._choose_project.assert_not_called()
        chrome.owner._navigate.assert_not_called()

    def test_project_picker_event_pump_cannot_open_a_nested_picker(self):
        chrome = self.make_chrome()
        self.queue(chrome, "choose-project")
        self.queue(chrome, "choose-project")
        inside_picker = False

        def choose_project(event):
            nonlocal inside_picker
            self.assertFalse(inside_picker, "Nested project picker can change the wrong project")
            inside_picker = True
            chrome.flush()  # A modal file picker pumps wx timer callbacks.
            inside_picker = False

        chrome.owner._choose_project.side_effect = choose_project
        chrome.flush()
        self.assertGreaterEqual(chrome.owner._choose_project.call_count, 1)

    def test_queue_is_bounded_during_event_storm_and_ignored_after_owner_closes(self):
        chrome = self.make_chrome()
        for _ in range(1000):
            self.queue(chrome, "choose-project")
        self.assertLessEqual(len(chrome.pending_actions), 8)
        chrome.owner._closed = True
        chrome.flush()
        chrome.owner._choose_project.assert_not_called()
        for view in chrome.views:
            view.RunScript.assert_not_called()

    def test_navigation_controls_cannot_bypass_owner_url_validation(self):
        chrome = self.make_chrome()
        self.queue(chrome, "navigate", value="  file:///C:/private/file  ")
        chrome.flush()
        chrome.owner.address.ChangeValue.assert_called_once_with("file:///C:/private/file")
        chrome.owner._navigate.assert_called_once_with(None)
        chrome.owner.webview.LoadURL.assert_not_called()

    def test_local_shell_navigation_vetoes_external_documents(self):
        chrome = self.make_chrome()
        view = chrome.views[0]
        for url in ("https://example.org", "file:///C:/private/file", "javascript:alert(1)"):
            navigation = SimpleNamespace(GetURL=lambda: url, Veto=Mock())
            chrome.on_navigating(view, navigation)
            navigation.Veto.assert_called_once()

    def test_control_changes_queue_state_without_running_scripts_and_bound_log_history(self):
        chrome = self.make_chrome()
        chrome.ready.update(chrome.views)
        status = chrome.control("status")
        status.SetLabel("Downloading C2040")
        self.assertEqual(status.GetLabel(), "Downloading C2040")
        address = chrome.control("address")
        address.ChangeValue("https://item.szlcsc.com/2392.html")
        self.assertEqual(address.GetValue(), chrome.model["address"])
        project_button = chrome.control("projectEnabled")
        project_button.Disable()
        self.assertTrue(chrome.model["busy"])
        project_button.Enable()
        self.assertFalse(chrome.model["busy"])
        log = chrome.control("logs")
        for index in range(1000):
            log.AppendText(f"{index:04d}: " + "x" * 60 + "\n")
        self.assertLessEqual(len(log.GetValue()), 30000)
        self.assertLessEqual(len(chrome.model["logs"]), 160)
        self.assertTrue(chrome.model["logs"][-1].startswith("0999:"))
        self.assertNotIn("0000:", log.GetValue())
        for view in chrome.views:
            view.RunScript.assert_not_called()


if __name__ == "__main__":
    unittest.main()
