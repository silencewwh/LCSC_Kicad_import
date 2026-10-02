"""Project import tests use isolated folders; no user KiCad settings are modified."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
import lcsc_project_import as importer  # noqa: E402


class ProjectImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve()
        self.converter = patch.object(
            importer, "_convert_component", side_effect=self.fake_converter
        )
        self.convert = self.converter.start()
        self.addCleanup(self.converter.stop)

    @staticmethod
    def fake_converter(component_id, directory, library_name, print_func):
        symbol = directory / f"{library_name}.kicad_sym"
        footprint = directory / f"{library_name}.pretty" / "TEST.kicad_mod"
        symbol.write_text('(kicad_symbol_lib (version 20231120) (symbol "TEST"))', encoding="utf-8")
        footprint.parent.mkdir()
        footprint.write_text('(footprint "TEST" (version 20240108))', encoding="utf-8")
        return SimpleNamespace(
            symbol_lib=symbol, footprint_file=footprint, model_wrl=None, model_step=None
        )

    def run_import(self, component="C2040", **kwargs):
        return importer.import_component_to_project(
            component, self.project, lambda message: None, **kwargs
        )

    def test_install_without_3d_registers_both_project_tables(self):
        result = self.run_import(" c2040 ")
        self.assertEqual(result.component_id, "C2040")
        self.assertEqual(result.library_name, "LCSC")
        self.assertEqual(result.directory, self.project / "LCSC")
        self.assertFalse(result.already_present)
        self.assertFalse(result.has_3d)
        for name, extension in (("sym-lib-table", "kicad_sym"), ("fp-lib-table", "pretty")):
            content = (self.project / name).read_text(encoding="utf-8")
            self.assertIn(f"${{KIPRJMOD}}/LCSC/LCSC.{extension}", content)
            self.assertNotIn(str(self.project), content)
        self.assertFalse((self.project / "kicad_common.json").exists())
        self.assertTrue((self.project / "LCSC" / ".import.lock").is_file())

    def test_duplicate_skips_download_and_repairs_missing_registration(self):
        self.run_import()
        (self.project / "fp-lib-table").unlink()
        result = self.run_import()
        self.assertTrue(result.already_present)
        self.assertEqual(self.convert.call_count, 1)
        self.assertTrue((self.project / "fp-lib-table").exists())
        symbol_table = (self.project / "sym-lib-table").read_text(encoding="utf-8")
        self.assertEqual(symbol_table.count('(name "LCSC")'), 1)

    def test_3d_result_reports_available_model(self):
        def with_model(*args):
            paths = self.fake_converter(*args)
            paths.model_step = args[1] / f"{args[2]}.3dshapes" / "TEST.step"
            paths.model_step.parent.mkdir()
            paths.model_step.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;")
            return paths

        self.convert.side_effect = with_model
        self.assertTrue(self.run_import().has_3d)
        self.assertTrue(self.run_import().has_3d)

    def test_damaged_shared_library_is_preserved(self):
        result = self.run_import()
        symbol = result.directory / "LCSC.kicad_sym"
        symbol.write_text("damaged", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_import()
        self.assertEqual(self.convert.call_count, 1)
        self.assertEqual(symbol.read_text(), "damaged")

    def test_missing_owned_footprint_is_reimported(self):
        result = self.run_import()
        (result.directory / "LCSC.pretty" / "C2040_TEST.kicad_mod").unlink()
        self.assertFalse(self.run_import().already_present)
        self.assertEqual(self.convert.call_count, 2)

    def test_different_components_share_one_library_without_collisions(self):
        first = self.run_import("C2040")
        second = self.run_import("C2041")
        self.assertEqual(first.library_name, second.library_name)
        self.assertEqual(first.directory, second.directory)
        for component in ("C2040", "C2041"):
            self.assertTrue(
                (first.directory / "LCSC.pretty" / f"{component}_TEST.kicad_mod").exists()
            )
            self.assertIn(
                f'(symbol "{component}_TEST")', (first.directory / "LCSC.kicad_sym").read_text()
            )
        for table in ("sym-lib-table", "fp-lib-table"):
            self.assertEqual((self.project / table).read_text().count('(name "LCSC")'), 1)

    def test_no_downloaded_model_removes_footprint_reference(self):
        class FakeImporter:
            def __init__(self, config, print_func):
                self.api = None

            def _import_3d_model(self, cad):
                return None, None

            def _import_footprint(self, footprint, model_ext):
                return footprint.model_3d

            def import_component(self, component):
                self._import_3d_model({})
                footprint = SimpleNamespace(
                    info=SimpleNamespace(name="TEST"), model_3d=SimpleNamespace(name="MODEL")
                )
                return self._import_footprint(footprint)

        fake_converter = SimpleNamespace(
            EasyEDAImporter=FakeImporter,
            Easyeda3dModelImporter=lambda **kwargs: SimpleNamespace(
                output=SimpleNamespace(name="MODEL")
            ),
            ImportConfig=lambda **kwargs: SimpleNamespace(**kwargs),
        )
        self.converter.stop()
        with patch.dict(sys.modules, {"impart_easyeda": fake_converter}):
            self.assertIsNone(importer._convert_component("C2040", self.project, "LCSC", print))

    def test_existing_tables_preserved_and_backed_up(self):
        existing = (
            b"(sym_lib_table\r\n  (version 7)\r\n  ; custom library\r\n"
            b'  (lib (name "User")(type "KiCad")(uri "${KIPRJMOD}/user.kicad_sym")'
            b'(options "x")(descr "User \\"quote\\"")(disabled)(future_field "keep"))\r\n)\r\n'
        )
        path = self.project / "sym-lib-table"
        path.write_bytes(existing)
        self.run_import()
        new = path.read_bytes()
        self.assertTrue(new.startswith(existing[:-3]))
        backups = list(self.project.glob("sym-lib-table.lcsc-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), existing)

    def test_nickname_collision_fails_before_download(self):
        data = b'(sym_lib_table (lib (name "LCSC")(type "KiCad")(uri "/other.kicad_sym")))'
        (self.project / "sym-lib-table").write_bytes(data)
        with self.assertRaisesRegex(ValueError, "another library"):
            self.run_import()
        self.convert.assert_not_called()
        self.assertEqual((self.project / "sym-lib-table").read_bytes(), data)

    def test_partial_conversion_installs_nothing(self):
        def missing_footprint(*args):
            paths = self.fake_converter(*args)
            paths.footprint_file = None
            return paths

        self.convert.side_effect = missing_footprint
        with self.assertRaisesRegex(RuntimeError, "both a symbol and a footprint"):
            self.run_import()
        self.assertEqual(self.import_assets(), [])
        self.assertFalse((self.project / "sym-lib-table").exists())
        self.assertEqual(self.import_assets(), [])

    def test_unowned_directory_contents_are_preserved(self):
        directory = self.project / "LCSC"
        directory.mkdir()
        user_file = directory / "notes.txt"
        user_file.write_text("mine", encoding="utf-8")
        self.run_import(overwrite=True)
        self.assertEqual(user_file.read_text(), "mine")

    def test_unowned_files_survive_explicit_overwrite(self):
        result = self.run_import()
        notes = result.directory / "notes.txt"
        notes.write_text("mine", encoding="utf-8")
        self.run_import(overwrite=True)
        self.assertEqual(notes.read_text(), "mine")
        manifest = json.loads((result.directory / importer._MANIFEST).read_text())
        self.assertNotIn("notes.txt", manifest["components"]["C2040"]["files"])
        self.assertEqual(self.convert.call_count, 2)

    def test_registration_failure_rolls_back_tables_and_new_component(self):
        symbol_original = b"(sym_lib_table (version 7))\n"
        footprint_original = b"(fp_lib_table (version 7))\n"
        (self.project / "sym-lib-table").write_bytes(symbol_original)
        (self.project / "fp-lib-table").write_bytes(footprint_original)
        real_write = importer._atomic_write
        failed = False

        def fail_second_table(path, content):
            nonlocal failed
            if path.name == "fp-lib-table" and not failed:
                failed = True
                raise OSError("simulated disk failure")
            real_write(path, content)

        with patch.object(importer, "_atomic_write", side_effect=fail_second_table):
            with self.assertRaisesRegex(OSError, "simulated disk failure"):
                self.run_import()
        self.assertEqual((self.project / "sym-lib-table").read_bytes(), symbol_original)
        self.assertEqual((self.project / "fp-lib-table").read_bytes(), footprint_original)
        self.assertEqual(self.import_assets(), [])

    def test_failed_overwrite_restores_existing_component(self):
        result = self.run_import()
        old_symbol = (result.directory / "LCSC.kicad_sym").read_bytes()
        (self.project / "fp-lib-table").unlink()
        with patch.object(importer, "_atomic_write", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                self.run_import(overwrite=True)
        self.assertEqual((result.directory / "LCSC.kicad_sym").read_bytes(), old_symbol)
        self.assertTrue((result.directory / importer._MANIFEST).exists())
        self.assertFalse((self.project / "fp-lib-table").exists())

    def test_table_changed_during_download_is_preserved(self):
        external_edit = b"(sym_lib_table (version 7))\n"

        def edit_during_conversion(*args):
            paths = self.fake_converter(*args)
            (self.project / "sym-lib-table").write_bytes(external_edit)
            return paths

        self.convert.side_effect = edit_during_conversion
        with self.assertRaisesRegex(RuntimeError, "changed during import"):
            self.run_import()
        self.assertEqual((self.project / "sym-lib-table").read_bytes(), external_edit)
        self.assertEqual(self.import_assets(), [])

    def test_traversal_input_and_unsafe_names_are_rejected(self):
        for component in ("C2040/../C1", "C0", "C2040?x", "../C2040", "C2040\nC2041"):
            with self.subTest(component=component), self.assertRaises(ValueError):
                self.run_import(component)
        for name in ("../foo", "C:/bad", "..", "CON", "name."):
            with self.subTest(name=name), self.assertRaises(ValueError):
                importer._safe_name(name)
        self.convert.assert_not_called()

    def test_converter_output_outside_staging_is_rejected(self):
        external = self.project / "outside.kicad_mod"
        external.write_text('(footprint "outside")', encoding="utf-8")

        def outside_path(*args):
            paths = self.fake_converter(*args)
            paths.footprint_file = external
            return paths

        self.convert.side_effect = outside_path
        with self.assertRaises(ValueError):
            self.run_import()
        self.assertTrue(external.exists())
        self.assertEqual(self.import_assets(), [])

    def test_malformed_table_fails_without_downloading(self):
        (self.project / "sym-lib-table").write_text(
            '(sym_lib_table (lib (name "bad"))', encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            self.run_import()
        self.convert.assert_not_called()

    def test_lock_prevents_simultaneous_import(self):
        root = self.project / "LCSC"
        root.mkdir()
        lock = root / ".import.lock"
        with importer._project_lock(lock):
            with self.assertRaisesRegex(RuntimeError, "Another project import"):
                self.run_import()
        self.convert.assert_not_called()

    def test_existing_shared_symbols_comments_and_unknown_fields_are_preserved(self):
        directory = self.project / "LCSC"
        directory.mkdir()
        original = (
            "(kicad_symbol_lib\r\n (version 20250101)\r\n"
            ' ; User note: preserve (this) exactly\r\n (future_field "keep")\r\n'
            ' (symbol "User\\"Symbol" (property "Value" "original"))\r\n)\r\n'
        )
        path = directory / "LCSC.kicad_sym"
        path.write_bytes(original.encode())
        self.run_import()
        self.assertTrue(path.read_bytes().startswith(original[:-3].encode()))
        self.assertIn('(symbol "C2040_TEST")', path.read_text())

    def test_existing_symbol_collision_keeps_every_file_unchanged(self):
        directory = self.project / "LCSC"
        directory.mkdir()
        path = directory / "LCSC.kicad_sym"
        original = b'(kicad_symbol_lib (symbol "C2040_TEST" (property "Value" "mine")))'
        path.write_bytes(original)
        with self.assertRaisesRegex(FileExistsError, "Symbol already exists"):
            self.run_import()
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((directory / "LCSC.pretty").exists())
        self.assertFalse((self.project / "sym-lib-table").exists())

    def test_existing_footprint_collision_keeps_every_file_unchanged(self):
        self.run_import("C2041")
        path = self.project / "LCSC" / "LCSC.pretty" / "C2040_TEST.kicad_mod"
        path.write_text('(footprint "mine")')
        before = self.project_snapshot()
        with self.assertRaisesRegex(FileExistsError, "existing or edited file"):
            self.run_import()
        self.assertEqual(self.project_snapshot(), before)

    def test_symbols_units_inheritance_footprint_and_models_use_shared_names(self):
        def with_references(*args):
            paths = self.fake_converter(*args)
            paths.symbol_lib.write_text(
                "(kicad_symbol_lib (version 20231120)"
                ' (symbol "TEST" (property "Footprint" "Old:TEST")'
                ' (symbol "TEST_0_1") (symbol "TEST_1_1"))'
                ' (symbol "DERIVED" (extends "TEST")))'
            )
            paths.model_step = args[1] / "LCSC.3dshapes" / "MODEL.step"
            paths.model_step.parent.mkdir()
            paths.model_step.write_bytes(b"model")
            paths.footprint_file.write_text(
                '(footprint "TEST" (model "${OLD}/OLD.3dshapes/MODEL.step" (offset (xyz 1 2 3))))'
            )
            return paths

        self.convert.side_effect = with_references
        self.run_import()
        directory = self.project / "LCSC"
        symbol = (directory / "LCSC.kicad_sym").read_text()
        for expected in (
            '(symbol "C2040_TEST"',
            '(symbol "C2040_TEST_0_1")',
            '(symbol "C2040_TEST_1_1")',
            '(symbol "C2040_DERIVED" (extends "C2040_TEST"))',
            '(property "Footprint" "LCSC:C2040_TEST")',
        ):
            self.assertIn(expected, symbol)
        footprint = (directory / "LCSC.pretty" / "C2040_TEST.kicad_mod").read_text()
        self.assertIn('(footprint "C2040_TEST"', footprint)
        self.assertIn("${KIPRJMOD}/LCSC/LCSC.3dshapes/C2040_MODEL.step", footprint)
        self.assertIn("(offset (xyz 1 2 3))", footprint)
        self.assertEqual((directory / "LCSC.3dshapes" / "C2040_MODEL.step").read_bytes(), b"model")

    def test_duplicate_preserves_user_edits_but_explicit_overwrite_rejects_them(self):
        self.run_import()
        path = self.project / "LCSC" / "LCSC.kicad_sym"
        path.write_text(
            path.read_text().replace(
                '(symbol "C2040_TEST")', '(symbol "C2040_TEST" (custom "edited"))'
            )
        )
        before = self.project_snapshot()
        self.assertTrue(self.run_import().already_present)
        self.assertEqual(self.convert.call_count, 1)
        with self.assertRaisesRegex(FileExistsError, "symbol was edited"):
            self.run_import(overwrite=True)
        self.assertEqual(self.project_snapshot(), before)

    def test_asset_failure_restores_all_other_components_and_original_manifest(self):
        self.run_import("C2041")
        before = self.project_snapshot()
        real_write = importer._atomic_write

        def fail_manifest(path, content):
            if path.name == importer._MANIFEST:
                raise OSError("manifest failure")
            real_write(path, content)

        with patch.object(importer, "_atomic_write", side_effect=fail_manifest):
            with self.assertRaisesRegex(OSError, "manifest failure"):
                self.run_import()
        self.assertEqual(self.project_snapshot(), before)

    def test_failed_overwrite_restores_all_shared_assets(self):
        self.run_import()
        self.run_import("C2041")
        (self.project / "fp-lib-table").unlink()
        before = self.project_snapshot()
        real_write = importer._atomic_write

        def fail_table(path, content):
            if path.name == "fp-lib-table":
                raise OSError("table failure")
            real_write(path, content)

        with patch.object(importer, "_atomic_write", side_effect=fail_table):
            with self.assertRaisesRegex(OSError, "table failure"):
                self.run_import(overwrite=True)
        self.assertEqual(self.project_snapshot(), before)

    def test_external_shared_symbol_edit_during_download_is_not_overwritten(self):
        self.run_import("C2041")
        path = self.project / "LCSC" / "LCSC.kicad_sym"
        external_edit = path.read_bytes() + b"; external change\n"

        def edit_during_conversion(*args):
            paths = self.fake_converter(*args)
            path.write_bytes(external_edit)
            return paths

        self.convert.side_effect = edit_during_conversion
        with self.assertRaisesRegex(RuntimeError, "changed during import"):
            self.run_import()
        self.assertEqual(path.read_bytes(), external_edit)
        self.assertFalse((path.parent / "LCSC.pretty" / "C2040_TEST.kicad_mod").exists())

    def test_legacy_component_is_copied_without_download_and_keeps_old_library(self):
        legacy = self.project / "lcsc" / "C2040"
        legacy.mkdir(parents=True)
        paths = self.fake_converter("C2040", legacy, "LCSC_C2040", print)
        manifest = importer._validate_assets(legacy, "LCSC_C2040", paths)
        manifest["component_id"] = "C2040"
        (legacy / importer._MANIFEST).write_text(json.dumps(manifest))
        old_symbol = paths.symbol_lib.read_bytes()
        old_table = b'(sym_lib_table (lib (name "LCSC_C2040")(type "KiCad")(uri "${KIPRJMOD}/lcsc/C2040/LCSC_C2040.kicad_sym")))'
        (self.project / "sym-lib-table").write_bytes(old_table)
        result = self.run_import()
        self.assertFalse(result.already_present)
        self.convert.assert_not_called()
        self.assertEqual(paths.symbol_lib.read_bytes(), old_symbol)
        self.assertIn('(symbol "C2040_TEST")', (result.directory / "LCSC.kicad_sym").read_text())
        self.assertTrue((self.project / "sym-lib-table").read_bytes().startswith(old_table[:-1]))
        self.assertTrue(self.run_import().already_present)
        self.convert.assert_not_called()

    def test_missing_shared_library_with_other_components_is_not_silently_recreated(self):
        self.run_import("C2041")
        (self.project / "LCSC" / "LCSC.kicad_sym").unlink()
        with self.assertRaisesRegex(FileNotFoundError, "restore it"):
            self.run_import()
        self.assertEqual(self.convert.call_count, 1)

    def model_converter(self, status):
        def convert(*args):
            paths = self.fake_converter(*args)
            paths.model_status = status
            paths.footprint_file.write_text('(footprint "TEST" (descr "original geometry"))')
            if status == "available":
                paths.model_step = args[1] / "LCSC.3dshapes" / "MODEL.step"
                paths.model_step.parent.mkdir()
                paths.model_step.write_bytes(b"model")
                paths.footprint_file.write_text(
                    '(footprint "TEST" (descr "remote geometry")'
                    ' (model "${OLD}/MODEL.step" (offset (xyz 1 2 3))))'
                )
            return paths

        return convert

    def test_failed_models_are_retried_without_replacing_symbol_or_footprint_geometry(self):
        self.convert.side_effect = self.model_converter("failed")
        first = self.run_import()
        self.assertFalse(first.has_3d)
        symbol = first.directory / "LCSC.kicad_sym"
        symbol.write_text(symbol.read_text().replace('"C2040_TEST"', '"C2040_TEST" (custom "mine")'))
        before = symbol.read_bytes()
        self.convert.side_effect = self.model_converter("available")
        second = self.run_import()
        self.assertTrue(second.has_3d)
        self.assertFalse(second.already_present)
        self.assertEqual(symbol.read_bytes(), before)
        footprint = (first.directory / "LCSC.pretty" / "C2040_TEST.kicad_mod").read_text()
        self.assertIn('descr "original geometry"', footprint)
        self.assertNotIn("remote geometry", footprint)
        self.assertIn("C2040_MODEL.step", footprint)
        self.assertIn("(offset (xyz 1 2 3))", footprint)
        self.assertTrue(self.run_import().already_present)
        self.assertEqual(self.convert.call_count, 2)

    def test_repeated_model_failure_preserves_files_and_absent_metadata_stops_retry(self):
        self.convert.side_effect = self.model_converter("failed")
        self.run_import()
        before = self.project_snapshot()
        self.assertTrue(self.run_import().already_present)
        self.assertEqual(self.project_snapshot(), before)
        self.convert.side_effect = self.model_converter("unavailable")
        self.run_import()
        self.run_import()
        self.assertEqual(self.convert.call_count, 3)

    def test_model_retry_preserves_edited_footprint(self):
        self.convert.side_effect = self.model_converter("failed")
        result = self.run_import()
        footprint = result.directory / "LCSC.pretty" / "C2040_TEST.kicad_mod"
        footprint.write_text(footprint.read_text().replace("original geometry", "user geometry"))
        before = self.project_snapshot()
        self.convert.side_effect = self.model_converter("available")
        with self.assertRaisesRegex(FileExistsError, "footprint was edited"):
            self.run_import()
        self.assertEqual(self.project_snapshot(), before)

    def test_older_manifest_without_model_status_is_checked_once(self):
        result = self.run_import()
        path = result.directory / importer._MANIFEST
        manifest = json.loads(path.read_bytes())
        del manifest["components"]["C2040"]["model_status"]
        path.write_text(json.dumps(manifest))
        self.run_import()
        self.run_import()
        self.assertEqual(self.convert.call_count, 2)

    def crash_import(self, trigger, extra=""):
        code = textwrap.dedent("""
            import os, sys
            from pathlib import Path
            sys.path.insert(0, str(Path.cwd() / "plugins"))
            sys.path.insert(0, str(Path.cwd() / "tests"))
            import lcsc_project_import as imp
            from test_lcsc_project_import import ProjectImportTests
            imp._convert_component = ProjectImportTests.fake_converter
            real_write = imp._atomic_write
            def crash_write(path, content):
                real_write(path, content)
                if path.name == sys.argv[2]:
                    os._exit(91)
            imp._atomic_write = crash_write
        """) + extra + "\nimp.import_component_to_project('C2040', Path(sys.argv[1]), lambda _: None)\n"
        result = subprocess.run(
            [sys.executable, "-B", "-c", code, str(self.project), trigger],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 91, result.stderr.decode(errors="replace"))

    def test_process_exit_during_preparation_or_commit_is_recoverable(self):
        original_project = self.project
        for trigger in (
            "owner", "journal.json", "LCSC.kicad_sym", "C2040_TEST.kicad_mod",
            importer._MANIFEST, "sym-lib-table", "fp-lib-table", "committed",
        ):
            with self.subTest(trigger=trigger), tempfile.TemporaryDirectory() as temporary:
                self.project = Path(temporary).resolve()
                self.crash_import(trigger)
                self.run_import()
                directory = self.project / "LCSC"
                self.assertFalse((directory / importer._RECOVERY).exists())
                self.assertEqual(list(directory.glob(".journal-*")), [])
                symbols = importer._symbol_forms((directory / "LCSC.kicad_sym").read_text())
                self.assertEqual(set(symbols), {"C2040_TEST"})
                self.assertTrue(self.run_import().already_present)
        self.project = original_project

    def test_recovery_refuses_to_overwrite_edits_made_after_crash(self):
        self.crash_import("LCSC.kicad_sym")
        path = self.project / "LCSC" / "LCSC.kicad_sym"
        edited = path.read_bytes() + b"; user edit after crash\n"
        path.write_bytes(edited)
        with self.assertRaisesRegex(RuntimeError, "edited after interrupted import"):
            self.run_import()
        self.assertEqual(path.read_bytes(), edited)
        self.assertTrue((path.parent / importer._RECOVERY).is_dir())

    def test_interrupted_cleanup_after_commit_needs_no_backup_files(self):
        self.crash_import("never", textwrap.dedent("""
            def interrupted_cleanup(directory):
                if directory.name == imp._RECOVERY:
                    (directory / "journal.json").unlink()
                    os._exit(91)
            imp._remove_recovery = interrupted_cleanup
        """))
        self.assertTrue(self.run_import().already_present)
        self.assertFalse((self.project / "LCSC" / importer._RECOVERY).exists())

    def test_interrupted_cleanup_after_rollback_needs_no_backup_files(self):
        self.crash_import("never", textwrap.dedent("""
            def fail_table(path, content):
                if path.name == "fp-lib-table":
                    raise OSError("table failure")
                real_write(path, content)
            imp._atomic_write = fail_table
            def interrupted_cleanup(directory):
                if directory.name == imp._RECOVERY:
                    (directory / "journal.json").unlink()
                    os._exit(91)
            imp._remove_recovery = interrupted_cleanup
        """))
        self.assertFalse(self.run_import().already_present)
        self.assertFalse((self.project / "LCSC" / importer._RECOVERY).exists())

    def test_recovery_rejects_journal_paths_outside_managed_files(self):
        self.crash_import("LCSC.kicad_sym")
        path = self.project / "LCSC" / importer._RECOVERY / "journal.json"
        journal = json.loads(path.read_bytes())
        journal["files"][0]["path"] = "user.txt"
        path.write_text(json.dumps(journal))
        user_file = self.project / "user.txt"
        user_file.write_text("user content")
        with self.assertRaisesRegex(RuntimeError, "Unexpected path"):
            self.run_import()
        self.assertEqual(user_file.read_text(), "user content")

    def test_work_cleanup_error_still_releases_lock(self):
        with patch.object(importer.shutil, "rmtree", side_effect=OSError("cleanup failed")):
            with self.assertRaisesRegex(OSError, "cleanup failed"):
                self.run_import()
        self.assertTrue(self.run_import().already_present)

    def test_old_unheld_pid_file_does_not_block_new_import(self):
        directory = self.project / "LCSC"
        directory.mkdir()
        (directory / ".import.lock").write_text("999999")
        self.assertFalse(self.run_import().already_present)

    def project_snapshot(self):
        return {
            path.relative_to(self.project).as_posix(): path.read_bytes()
            for path in self.project.rglob("*")
            if path.is_file() and ".lcsc-" not in path.name
        }

    def import_assets(self):
        return [path for path in (self.project / "LCSC").iterdir() if path.name != ".import.lock"]


if __name__ == "__main__":
    unittest.main()
