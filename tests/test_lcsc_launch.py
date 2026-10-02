"""Startup failures must remain visible when KiCad launches console-free Python."""

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
import lcsc_launch as launch  # noqa: E402


class LaunchErrorTests(unittest.TestCase):
    def test_success_does_not_write_or_show_error(self):
        browser = SimpleNamespace(main=Mock())
        with (
            patch.dict(sys.modules, {"lcsc_browser": browser}),
            patch.object(sys, "argv", ["lcsc_launch.py"]),
            patch.object(launch, "_report_startup_error") as report,
        ):
            self.assertEqual(launch.main(), 0)
        browser.main.assert_called_once_with()
        report.assert_not_called()

    def test_constructor_failure_returns_failure_and_reports_original_error(self):
        failure = RuntimeError("界面加载失败")
        browser = SimpleNamespace(main=Mock(side_effect=failure))
        with (
            patch.dict(sys.modules, {"lcsc_browser": browser}),
            patch.object(sys, "argv", ["lcsc_launch.py"]),
            patch.object(launch, "_report_startup_error") as report,
        ):
            self.assertEqual(launch.main(), 1)
        report.assert_called_once_with(failure)

    def test_import_failure_is_reported_before_wx_is_loaded(self):
        with (
            patch.dict(sys.modules, {"lcsc_browser": None}),
            patch.object(sys, "argv", ["lcsc_launch.py"]),
            patch.object(launch, "_report_startup_error") as report,
        ):
            self.assertEqual(launch.main(), 1)
        self.assertIsInstance(report.call_args.args[0], ModuleNotFoundError)

    def test_log_preserves_unicode_traceback_and_redacts_ipc_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "logs"
            with (
                patch.object(launch, "_error_log_directories", return_value=[directory]),
                patch.dict(
                    os.environ,
                    {"KICAD_API_TOKEN": "test-secret", "KICAD_API_SOCKET": "ipc://test-socket"},
                ),
            ):
                try:
                    raise RuntimeError("界面失败 test-secret ipc://test-socket")
                except RuntimeError as error:
                    path = launch._write_startup_error(error)
            text = path.read_text(encoding="utf-8")
        self.assertIn("Traceback", text)
        self.assertIn("界面失败", text)
        self.assertIn("[redacted]", text)
        self.assertNotIn("test-secret", text)
        self.assertNotIn("ipc://test-socket", text)

    def test_log_uses_fallback_when_primary_directory_is_unwritable(self):
        with tempfile.TemporaryDirectory() as temporary:
            blocked = Path(temporary) / "not-a-directory"
            blocked.write_text("fixture", encoding="utf-8")
            fallback = Path(temporary) / "fallback"
            with patch.object(
                launch, "_error_log_directories", return_value=[blocked / "logs", fallback]
            ):
                path = launch._write_startup_error(RuntimeError("startup failed"))
            self.assertEqual(path, fallback / "launch-error.log")
            self.assertIn("startup failed", path.read_text(encoding="utf-8"))

    def test_all_log_failures_still_show_original_error(self):
        with (
            patch.object(launch, "_error_log_directories", return_value=[Path("unwritable")]),
            patch.object(Path, "mkdir", side_effect=PermissionError("denied")),
            patch.object(launch, "_show_startup_error") as show,
        ):
            launch._report_startup_error(RuntimeError("界面加载失败"))
        message = show.call_args.args[0]
        self.assertIn("界面加载失败", message)
        self.assertIn("错误记录无法写入", message)

    def test_windows_native_prompt_does_not_need_wx(self):
        message_box = Mock(return_value=1)
        native = SimpleNamespace(
            windll=SimpleNamespace(user32=SimpleNamespace(MessageBoxW=message_box))
        )
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"ctypes": native, "wx": None}),
        ):
            launch._show_startup_error("启动失败")
        self.assertEqual(message_box.call_args.args[1], "启动失败")

    def test_unexpected_log_failure_does_not_hide_startup_failure(self):
        with (
            patch.object(launch, "_write_startup_error", side_effect=ValueError("bad log path")),
            patch.object(launch, "_show_startup_error") as show,
        ):
            launch._report_startup_error(RuntimeError("original failure"))
        self.assertIn("original failure", show.call_args.args[0])
        self.assertIn("错误记录无法写入", show.call_args.args[0])

    def test_native_prompt_failure_falls_back_to_stderr(self):
        output = io.StringIO()
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"ctypes": None}),
            patch.object(sys, "stderr", output),
        ):
            launch._show_startup_error("启动失败")
        self.assertIn("启动失败", output.getvalue())

    def test_pythonw_with_no_streams_does_not_raise_secondary_error(self):
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"ctypes": None}),
            patch.object(sys, "stderr", None),
            patch.object(sys, "__stderr__", None),
        ):
            launch._show_startup_error("启动失败")


if __name__ == "__main__":
    unittest.main()
