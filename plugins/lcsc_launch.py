"""IPC entrypoint shared by the schematic and PCB editors.

The launcher never imports pcbnew. KiCad supplies KICAD_API_SOCKET and
KICAD_API_TOKEN to the action process, allowing the browser to obtain the
project from the editor which launched it. ``--check`` checks this exact
entrypoint and its runtime dependencies without opening any windows.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent


def _redact(text: str) -> str:
    """Never include the launching editor's credentials in an error report."""
    for key in ("KICAD_API_TOKEN", "KICAD_API_SOCKET"):
        value = os.environ.get(key)
        if value:
            text = text.replace(value, "[redacted]")
    return text


def _error_log_directories() -> list[Path]:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Logs"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return [base / "LCSCKiCadBridge/logs", Path(tempfile.gettempdir()) / "LCSCKiCadBridge/logs"]


def _write_startup_error(error: Exception) -> Path | None:
    """Append UTF-8 diagnostics outside the installed plugin, with a temp fallback."""
    details = _redact("".join(traceback.format_exception(type(error), error, error.__traceback__)))
    record = f"\n[{datetime.now(timezone.utc).isoformat()}] Startup failed\n{details}\n"
    try:
        directories = _error_log_directories()
    except Exception:
        return None
    for directory in directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "launch-error.log"
            with path.open("a", encoding="utf-8", errors="replace") as stream:
                stream.write(record)
            return path
        except OSError:
            continue
    return None


def _show_startup_error(message: str) -> None:
    """Windows pythonw has no console; the native dialog works without wxPython."""
    if sys.platform == "win32":
        try:
            import ctypes

            if ctypes.windll.user32.MessageBoxW(None, message, "立创元件桥 · 启动失败", 0x10):
                return
        except Exception:
            pass
    else:
        try:
            import wx

            app = wx.App.Get()
            owns_app = app is None
            if owns_app:
                app = wx.App(False)
            try:
                wx.MessageBox(message, "立创元件桥 · 启动失败", wx.OK | wx.ICON_ERROR)
            finally:
                if owns_app:
                    app.Destroy()
            return
        except Exception:
            pass
    # A headless or damaged GUI runtime must not replace the original failure.
    try:
        stream = sys.stderr or sys.__stderr__
        if stream:
            stream.write(message + "\n")
            stream.flush()
    except Exception:
        pass


def _report_startup_error(error: Exception) -> None:
    try:
        path = _write_startup_error(error)
    except Exception:
        path = None
    summary = _redact(f"{type(error).__name__}: {error}")[:1500]
    location = f"错误记录：\n{path}" if path else "错误记录无法写入，请将此提示提供给开发者。"
    _show_startup_error(f"立创元件桥未能启动。\n\n{summary}\n\n{location}")


def check_runtime() -> dict:
    """Return actionable dependency checks without contacting or changing KiCad."""
    checks = {}
    for name in ("wx", "wx.html2", "kipy", "lcsc_browser", "lcsc_ui"):
        try:
            importlib.import_module(name)
            checks[name] = {"ok": True}
        except Exception as error:
            checks[name] = {"ok": False, "error": f"{type(error).__name__}: {error}"}
            if name.startswith("wx") and sys.platform == "win32":
                checks[name]["hint"] = (
                    "请在 KiCad 的插件设置中选择 KiCad 安装目录下的 bin/pythonw.exe。"
                )
    return {
        "ready": all(check["ok"] for check in checks.values()),
        "python": sys.version.split()[0],
        "ipc_connected_environment": bool(os.environ.get("KICAD_API_SOCKET")),
        "checks": checks,
    }


def main() -> int:
    if str(PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(PLUGIN_DIR))
    if "--check" in sys.argv[1:]:
        report = check_runtime()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ready"] else 1

    try:
        from lcsc_browser import main as open_browser

        open_browser()
    except Exception as error:
        _report_startup_error(error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
