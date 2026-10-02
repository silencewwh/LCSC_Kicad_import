"""Parallel model downloads preserve both formats and independent failures."""

from __future__ import annotations

import gzip
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
import impart_easyeda as converter  # noqa: E402

OBJ = """newmtl body
Ka 0.2 0.2 0.2
Kd 0.8 0.8 0.8
Ks 0.1 0.1 0.1
endmtl
v 0 0 0
v 1 0 0
v 0 1 0
usemtl body
f 1 2 3
"""
STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"


class ParallelModelsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.logs = []
        self.api = SimpleNamespace(
            get_raw_3d_model_obj=Mock(return_value=OBJ),
            get_step_3d_model=Mock(return_value=STEP),
            get_cad_data_of_component=Mock(return_value={"fixture": True}),
        )
        with patch.object(converter, "EasyedaApi", return_value=self.api):
            self.importer = converter.EasyEDAImporter(
                converter.ImportConfig(base_folder=self.directory), self.logs.append
            )
        self.model = SimpleNamespace(
            name="test-model",
            uuid="test-uuid",
            translation=SimpleNamespace(x=0, y=0, z=0),
            raw_obj=None,
            step=None,
        )
        self.metadata = patch.object(
            converter, "Easyeda3dModelImporter", return_value=SimpleNamespace(output=self.model)
        )
        self.parse_model = self.metadata.start()
        self.addCleanup(self.metadata.stop)

    def test_both_downloads_are_in_flight_before_either_returns(self):
        rendezvous = threading.Barrier(2)
        arrivals = []
        lock = threading.Lock()

        def fetch(kind, data):
            def download(*, uuid):
                with lock:
                    arrivals.append((kind, uuid, threading.get_ident()))
                # A timeout only prevents a hanging test. A serial implementation
                # cannot cross this barrier and cannot produce both model files.
                rendezvous.wait(timeout=5)
                return data

            return download

        self.api.get_raw_3d_model_obj.side_effect = fetch("obj", OBJ)
        self.api.get_step_3d_model.side_effect = fetch("step", STEP)
        wrl, step = self.importer._import_3d_model({"fixture": True})
        self.assertEqual({arrival[0] for arrival in arrivals}, {"obj", "step"})
        self.assertEqual(len({arrival[2] for arrival in arrivals}), 2)
        self.assertIn("#VRML V2.0", wrl.read_text(encoding="utf-8"))
        self.assertIn("0,1,2,-1", wrl.read_text(encoding="utf-8"))
        self.assertEqual(step.read_bytes(), STEP)
        self.assertEqual(self.importer._model_status, "available")
        self.parse_model.assert_called_once_with(
            easyeda_cp_cad_data={"fixture": True}, download_raw_3d_model=False, api=self.api
        )
        self.api.get_raw_3d_model_obj.assert_called_once_with(uuid="test-uuid")
        self.api.get_step_3d_model.assert_called_once_with(uuid="test-uuid")
        self.assertTrue(any("并行下载" in line for line in self.logs))

    def test_obj_download_exception_preserves_successful_step(self):
        self.api.get_raw_3d_model_obj.side_effect = OSError("OBJ unavailable")
        wrl, step = self.importer._import_3d_model({})
        self.assertIsNone(wrl)
        self.assertEqual(step.read_bytes(), STEP)
        self.assertTrue(any("OBJ 模型下载失败" in line for line in self.logs))
        self.assertEqual(self.importer._model_status, "available")

    def test_failed_model_downloads_are_marked_retryable(self):
        self.api.get_raw_3d_model_obj.side_effect = OSError("OBJ unavailable")
        self.api.get_step_3d_model.side_effect = OSError("STEP unavailable")
        self.assertEqual(self.importer._import_3d_model({}), (None, None))
        self.assertEqual(self.importer._model_status, "failed")

    def test_step_download_exception_preserves_successful_wrl(self):
        self.api.get_step_3d_model.side_effect = OSError("STEP unavailable")
        wrl, step = self.importer._import_3d_model({})
        self.assertIsNone(step)
        self.assertIn("#VRML V2.0", wrl.read_text(encoding="utf-8"))
        self.assertTrue(any("STEP 模型下载失败" in line for line in self.logs))

    def test_step_only_error_pages_and_truncated_data_are_not_saved(self):
        self.api.get_raw_3d_model_obj.return_value = None
        for invalid in (
            b"<html>temporary service error</html>",
            b'{"error":"temporarily unavailable"}',
            STEP[:40],
            STEP.replace(b"HEADER;", b""),
            "not bytes",
        ):
            with self.subTest(payload=invalid):
                self.api.get_step_3d_model.return_value = invalid
                self.assertEqual(self.importer._import_3d_model({}), (None, None))
                self.assertIsNone(self.model.step)
                self.assertFalse((self.importer.model_dir / "test-model.step").exists())

    def test_invalid_step_response_does_not_discard_valid_wrl(self):
        self.api.get_step_3d_model.return_value = b"<html>temporary service error</html>"
        wrl, step = self.importer._import_3d_model({})
        self.assertIsNone(step)
        self.assertIn("#VRML V2.0", wrl.read_text(encoding="utf-8"))
        self.assertFalse((self.importer.model_dir / "test-model.step").exists())
        self.assertTrue(any("不是完整的 STEP 模型" in line for line in self.logs))

    def test_obj_conversion_failure_preserves_step(self):
        with patch.object(converter, "Exporter3dModelKicad", side_effect=ValueError("bad OBJ")):
            wrl, step = self.importer._import_3d_model({})
        self.assertIsNone(wrl)
        self.assertEqual(step.read_bytes(), STEP)

    def test_empty_wrl_geometry_remains_retryable_without_step(self):
        self.api.get_step_3d_model.return_value = None
        for obj in (OBJ.replace("usemtl body", "usemtl missing"), OBJ.replace("f 1 2 3", "")):
            with self.subTest(obj=obj):
                self.api.get_raw_3d_model_obj.return_value = obj
                self.assertEqual(self.importer._import_3d_model({}), (None, None))
                self.assertEqual(self.importer._model_status, "failed")
                self.assertFalse((self.importer.model_dir / "test-model.wrl").exists())

    def test_empty_wrl_geometry_does_not_discard_valid_step(self):
        for obj in (OBJ.replace("usemtl body", "usemtl missing"), OBJ.replace("f 1 2 3", "")):
            with self.subTest(obj=obj):
                self.api.get_raw_3d_model_obj.return_value = obj
                wrl, step = self.importer._import_3d_model({})
                self.assertIsNone(wrl)
                self.assertEqual(step.read_bytes(), STEP)
                self.assertEqual(self.importer._model_status, "available")
                self.assertFalse((self.importer.model_dir / "test-model.wrl").exists())

    def test_compressed_step_is_preserved_without_obj(self):
        self.api.get_raw_3d_model_obj.return_value = None
        self.importer.config.compress_models = True
        wrl, step = self.importer._import_3d_model({})
        self.assertIsNone(wrl)
        self.assertEqual(step.suffixes[-2:], [".step", ".gz"])
        self.assertEqual(gzip.decompress(step.read_bytes()), STEP)

    def test_existing_model_files_are_not_overwritten(self):
        self.importer.model_dir.mkdir()
        wrl = self.importer.model_dir / "test-model.wrl"
        step = self.importer.model_dir / "test-model.step"
        wrl.write_text("existing WRL", encoding="utf-8")
        step.write_bytes(b"existing STEP")
        actual_wrl, actual_step = self.importer._import_3d_model({})
        self.assertEqual(actual_wrl, wrl)
        self.assertEqual(actual_step, step)
        self.assertEqual(wrl.read_text(encoding="utf-8"), "existing WRL")
        self.assertEqual(step.read_bytes(), b"existing STEP")

    def test_no_model_metadata_never_requests_model_network(self):
        self.parse_model.return_value.output = None
        self.assertEqual(self.importer._import_3d_model({}), (None, None))
        self.assertEqual(self.importer._model_status, "unavailable")
        self.api.get_raw_3d_model_obj.assert_not_called()
        self.api.get_step_3d_model.assert_not_called()

    def test_step_only_import_references_step_even_if_wrl_was_preferred(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                self.importer.config.compress_models = compressed
                extension = "step.gz" if compressed else "step"
                step = self.directory / f"model.{extension}"
                footprint = SimpleNamespace()
                with (
                    patch.object(
                        converter,
                        "EasyedaFootprintImporter",
                        return_value=SimpleNamespace(get_footprint=lambda: footprint),
                    ),
                    patch.object(self.importer, "_import_symbol", return_value=(True, "test")),
                    patch.object(self.importer, "_import_3d_model", return_value=(None, step)),
                    patch.object(
                        self.importer,
                        "_import_footprint",
                        return_value=self.directory / "test.kicad_mod",
                    ) as export_footprint,
                ):
                    result = self.importer.import_component("C2040")
                export_footprint.assert_called_once_with(footprint, model_ext=extension)
                self.assertEqual(result.model_step, step)
                self.assertIsNone(result.model_wrl)
                self.assertEqual(result.model_status, "available")
        self.assertTrue(any("正在下载 C2040 的符号与封装数据" in line for line in self.logs))
        self.assertTrue(any("正在转换符号与封装" in line for line in self.logs))
        self.assertTrue(any("器件文件已生成" in line for line in self.logs))


if __name__ == "__main__":
    unittest.main()
