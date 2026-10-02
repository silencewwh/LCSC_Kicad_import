"""Regression checks for the schematic-capable IPC package entrypoint."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins"
sys.path.insert(0, str(PLUGIN_DIR))
import lcsc_launch as launch  # noqa: E402


class PluginEntryTests(unittest.TestCase):
    def test_pcm_and_editor_manifest_use_one_ipc_identity(self):
        metadata = json.loads((ROOT / "metadata.json").read_text(encoding="utf-8"))
        manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["identifier"], "com.github.lcsc-kicad.browser")
        self.assertEqual(metadata["identifier"], manifest["identifier"])
        self.assertTrue(metadata["$schema"].endswith("/v2"))
        self.assertEqual(metadata["versions"][0]["runtime"], "ipc")
        self.assertEqual(manifest["runtime"]["type"], "python")
        self.assertEqual(len(manifest["actions"]), 1)
        action = manifest["actions"][0]
        self.assertEqual(set(action["scopes"]), {"schematic", "pcb"})
        self.assertTrue(action["show-button"])
        self.assertEqual(action["entrypoint"], "lcsc_launch.py")
        self.assertTrue((PLUGIN_DIR / action["entrypoint"]).is_file())

    def test_import_package_does_not_load_pcb_only_plugin_or_gui(self):
        # Use a clean interpreter because other tests may have imported wx.
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                (
                    "import sys; import plugins; "
                    "assert 'pcbnew' not in sys.modules; "
                    "assert 'wx' not in sys.modules; "
                    "assert 'impart_action' not in sys.modules"
                ),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_launcher_calls_browser_without_rewriting_ipc_environment(self):
        seen = []
        browser = SimpleNamespace(main=lambda: seen.append(os.environ["KICAD_API_SOCKET"]))
        with (
            patch.dict(sys.modules, {"lcsc_browser": browser}),
            patch.dict(os.environ, {"KICAD_API_SOCKET": "ipc://launching-schematic"}),
            patch.object(sys, "argv", ["lcsc_launch.py"]),
        ):
            self.assertEqual(launch.main(), 0)
        self.assertEqual(seen, ["ipc://launching-schematic"])

    def test_dependency_failure_is_reported_without_opening_gui(self):
        def load(name):
            if name == "wx.html2":
                raise ImportError("WebView is missing")
            return SimpleNamespace()

        output = io.StringIO()
        with (
            patch.object(launch.importlib, "import_module", side_effect=load),
            patch.object(launch.sys, "platform", "win32"),
            patch.dict(os.environ, {"KICAD_API_TOKEN": "must-not-be-printed"}),
            patch.object(sys, "argv", ["lcsc_launch.py", "--check"]),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(launch.main(), 1)
        report = json.loads(output.getvalue())
        self.assertFalse(report["ready"])
        self.assertIn("WebView is missing", report["checks"]["wx.html2"]["error"])
        self.assertIn("bin/pythonw.exe", report["checks"]["wx.html2"]["hint"])
        self.assertNotIn("must-not-be-printed", output.getvalue())

    @unittest.skipUnless(importlib.util.find_spec("wx"), "KiCad wxPython is required")
    def test_project_is_obtained_from_schematic_only_ipc_server(self):
        import lcsc_browser
        from kipy.proto.common.types import DocumentSpecifier, DocumentType

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "schematic-only.kicad_pro").write_text("{}", encoding="utf-8")
            calls = []

            def documents(kind):
                calls.append(kind)
                if kind == DocumentType.DOCTYPE_PCB:
                    raise RuntimeError("No PCB editor is open")
                return [
                    DocumentSpecifier(
                        type=DocumentType.DOCTYPE_SCHEMATIC,
                        project={"path": str(project)},
                    )
                ]

            client = SimpleNamespace(get_open_documents=documents)
            with (
                patch("kipy.KiCad", return_value=client),
                patch.dict(
                    os.environ, {"KICAD_API_SOCKET": "ipc://schematic-only", "KIPRJMOD": ""}
                ),
            ):
                self.assertEqual(lcsc_browser.detect_project(), project.resolve())
            self.assertIn(DocumentType.DOCTYPE_SCHEMATIC, calls)

    @unittest.skipUnless(importlib.util.find_spec("wx"), "KiCad wxPython is required")
    def test_schematic_absolute_filename_can_identify_saved_project(self):
        import lcsc_browser
        from kipy.proto.common.types import DocumentSpecifier, DocumentType

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "example.kicad_pro").write_text("{}", encoding="utf-8")
            schematic = project / "example.kicad_sch"
            schematic.write_text("(kicad_sch)", encoding="utf-8")
            document = DocumentSpecifier(
                type=DocumentType.DOCTYPE_SCHEMATIC, board_filename=str(schematic)
            )
            client = SimpleNamespace(
                get_open_documents=lambda kind: (
                    [document] if kind == DocumentType.DOCTYPE_SCHEMATIC else []
                )
            )
            with (
                patch("kipy.KiCad", return_value=client),
                patch.dict(
                    os.environ, {"KICAD_API_SOCKET": "ipc://schematic-only", "KIPRJMOD": ""}
                ),
            ):
                self.assertEqual(lcsc_browser.detect_project(), project.resolve())
                # This is the real KiCad 10.0.6 response shape. Never resolve a
                # bare filename relative to the plugin's working directory.
                document.board_filename = schematic.name
                self.assertIsNone(lcsc_browser.detect_project())
                document.board_filename = str(project / "missing.kicad_sch")
                self.assertIsNone(lcsc_browser.detect_project())
                document.board_filename = str(schematic)
                (project / "example.kicad_pro").unlink()
                self.assertIsNone(lcsc_browser.detect_project())

    @unittest.skipUnless(importlib.util.find_spec("wx"), "KiCad wxPython is required")
    def test_kicad_10_schematic_uses_validated_launch_project_environment(self):
        import lcsc_browser
        from kipy.proto.common.types import DocumentSpecifier, DocumentType

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "example.kicad_pro").write_text("{}", encoding="utf-8")
            (project / "example.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
            # Mirrors KiCad 10.0.6's actual schematic response: no project.path.
            document = DocumentSpecifier(
                type=DocumentType.DOCTYPE_SCHEMATIC, board_filename="example.kicad_sch"
            )
            client = SimpleNamespace(
                get_open_documents=lambda kind: (
                    [document] if kind == DocumentType.DOCTYPE_SCHEMATIC else []
                )
            )
            with (
                patch("kipy.KiCad", return_value=client),
                patch.dict(
                    os.environ,
                    {"KICAD_API_SOCKET": "ipc://schematic-only", "KIPRJMOD": str(project)},
                ),
            ):
                self.assertEqual(lcsc_browser.detect_project(), project.resolve())
                document.board_filename = "other-project.kicad_sch"
                self.assertIsNone(lcsc_browser.detect_project())
                document.board_filename = "../example.kicad_sch"
                self.assertIsNone(lcsc_browser.detect_project())
                document.board_filename = "example.kicad_sch"
                (project / "example.kicad_pro").unlink()
                self.assertIsNone(lcsc_browser.detect_project())


if __name__ == "__main__":
    unittest.main()
