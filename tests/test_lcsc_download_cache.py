"""The shared raw-download cache never requires network access in these tests."""

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
sys.path.insert(0, str(ROOT / "plugins/easyeda2kicad"))
import lcsc_download_cache as cache  # noqa: E402

CAD = json.dumps(
    {
        "success": True,
        "result": {
            "title": "测试器件",
            "dataStr": {"head": {"c_para": {"name": "测试器件"}}, "shape": ["P~pin"]},
        },
    },
    ensure_ascii=False,
)
OBJ = "# model\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"
STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n#1=CARTESIAN_POINT('',(0.,0.,0.));\nENDSEC;\nEND-ISO-10303-21;\n"


def response(payload):
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    return contextlib.nullcontext(SimpleNamespace(status=200, read=io.BytesIO(raw).read))


class DownloadCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "cache"
        self.logs = []
        self.ssl = patch.object(cache.CachedEasyedaApi, "_create_ssl_context", return_value=None)
        self.ssl.start()
        self.addCleanup(self.ssl.stop)
        self.network = patch(
            "urllib.request.urlopen", side_effect=AssertionError("Network forbidden")
        )
        self.network.start()
        self.addCleanup(self.network.stop)
        self.api = cache.CachedEasyedaApi(cache_dir=self.root, print_func=self.logs.append)

    def test_upstream_requests_fill_cache_and_other_instance_reuses_all_formats(self):
        with patch(
            "urllib.request.urlopen", side_effect=[response(CAD), response(OBJ), response(STEP)]
        ) as fetch:
            self.assertEqual(self.api.get_cad_data_of_component("C2040")["title"], "测试器件")
            self.assertEqual(self.api.get_raw_3d_model_obj("model-uuid"), OBJ)
            self.assertEqual(self.api.get_step_3d_model("model-uuid"), STEP)
            self.assertEqual(fetch.call_count, 3)
        another_project = cache.CachedEasyedaApi(cache_dir=self.root, print_func=self.logs.append)
        self.assertEqual(another_project.get_cad_data_of_component("C2040")["title"], "测试器件")
        self.assertEqual(another_project.get_raw_3d_model_obj("model-uuid"), OBJ)
        self.assertEqual(another_project.get_step_3d_model("model-uuid"), STEP)
        self.assertEqual(len(self.logs), 3)
        self.assertTrue(all("缓存" in line for line in self.logs))
        raw = self.api._get_cache_path("C2040", "json").read_bytes()
        self.assertIn("测试器件".encode(), raw)

    def test_keys_are_hashed_and_never_escape_cache_or_leak_urls(self):
        secret = "../../outside?token=do-not-log"
        path = self.api._get_cache_path(secret, "obj")
        self.assertEqual(path.parent, self.root)
        self.assertRegex(path.name, r"^lcsc-v1-[0-9a-f]{64}\.obj$")
        self.api._write_to_cache(path, OBJ)
        self.assertEqual(self.api._read_from_cache(path), OBJ)
        self.assertNotIn("do-not-log", "".join(self.logs))
        self.assertNotIn("outside", "".join(self.logs))

    def test_json_and_models_have_distinct_expiration_periods(self):
        now = time.time()
        for extension, payload, ttl in (
            ("json", CAD, cache.CAD_TTL),
            ("obj", OBJ, cache.MODEL_TTL),
            ("step", STEP, cache.MODEL_TTL),
        ):
            with self.subTest(extension=extension):
                path = self.api._get_cache_path("resource", extension)
                self.api._write_to_cache(path, payload, binary=extension == "step")
                os.utime(path, (now - ttl + 5, now - ttl + 5))
                with patch.object(cache.time, "time", return_value=now):
                    self.assertIsNotNone(
                        self.api._read_from_cache(path, binary=extension == "step")
                    )
                os.utime(path, (now - ttl - 1, now - ttl - 1))
                with patch.object(cache.time, "time", return_value=now):
                    self.assertIsNone(self.api._read_from_cache(path, binary=extension == "step"))

    def test_expired_or_corrupt_entry_fetches_fresh_data(self):
        path = self.api._get_cache_path("C2040", "json")
        for invalid in (b"{broken", b"\xffinvalid-utf8", b'{"success":false}', b""):
            with self.subTest(invalid=invalid):
                self.root.mkdir(exist_ok=True)
                path.write_bytes(invalid)
                with patch("urllib.request.urlopen", return_value=response(CAD)) as fetch:
                    self.assertEqual(
                        self.api.get_cad_data_of_component("C2040")["title"], "测试器件"
                    )
                    fetch.assert_called_once()
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), json.loads(CAD))
        old = time.time() - cache.CAD_TTL - 1
        os.utime(path, (old, old))
        with patch("urllib.request.urlopen", return_value=response(CAD)) as fetch:
            self.api.get_cad_data_of_component("C2040")
            fetch.assert_called_once()

    def test_empty_error_or_unrecognizable_data_is_never_cached(self):
        for extension, values in (
            ("json", ["", "{}", "[]", '{"success":false}', '{"result":{}}', "<html>error</html>"]),
            ("obj", ["", "<html>error</html>", "v 0 0 0", "v nan 0 0\nf 1 2 3"]),
            ("step", [b"", b"<html>error</html>", STEP[:30]]),
        ):
            for value in values:
                with self.subTest(extension=extension, value=value):
                    path = self.api._get_cache_path("bad", extension)
                    self.api._write_to_cache(path, value, binary=extension == "step")
                    self.assertFalse(path.exists())

    def test_failed_downloads_are_retried_and_never_cached(self):
        with (
            self.assertLogs(level="ERROR"),
            patch(
                "urllib.request.urlopen", side_effect=urllib.error.URLError("fixture offline")
            ) as fetch,
        ):
            self.assertEqual(self.api.get_cad_data_of_component("C2040"), {})
            self.assertIsNone(self.api.get_raw_3d_model_obj("uuid"))
            self.assertIsNone(self.api.get_step_3d_model("uuid"))
            self.assertIsNone(self.api.get_step_3d_model("uuid"))
            self.assertEqual(fetch.call_count, 4)
        self.assertFalse(self.root.exists())

    def test_unwritable_cache_does_not_fail_download(self):
        for failure in ("directory", "replace"):
            with self.subTest(failure=failure):
                context = (
                    patch.object(
                        self.api, "_safe_directory", side_effect=PermissionError("blocked")
                    )
                    if failure == "directory"
                    else patch.object(cache.os, "replace", side_effect=PermissionError("blocked"))
                )
                with context, patch("urllib.request.urlopen", return_value=response(CAD)) as fetch:
                    self.assertEqual(
                        self.api.get_cad_data_of_component("C2040")["title"], "测试器件"
                    )
                    fetch.assert_called_once()
                self.assertFalse(self.api._get_cache_path("C2040", "json").exists())
                self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_disabled_cache_does_not_read_or_write(self):
        path = self.api._get_cache_path("uuid", "obj")
        self.api._write_to_cache(path, OBJ)
        self.api.use_cache = False
        self.assertIsNone(self.api._read_from_cache(path))
        self.api._write_to_cache(path, OBJ + "# changed\n")
        self.assertEqual(path.read_text(encoding="utf-8"), OBJ)

    def test_size_limit_evicts_oldest_owned_files_and_preserves_other_files(self):
        api = cache.CachedEasyedaApi(cache_dir=self.root, max_bytes=len(OBJ.encode()) * 2)
        oldest = api._get_cache_path("oldest", "obj")
        recent = api._get_cache_path("recent", "obj")
        newest = api._get_cache_path("newest", "obj")
        api._write_to_cache(oldest, OBJ)
        os.utime(oldest, (1, 1))
        api._write_to_cache(recent, OBJ)
        unrelated = self.root / "user-data.obj"
        unrelated.write_text("untouched", encoding="utf-8")
        nested = self.root / "keep"
        nested.mkdir()
        (nested / oldest.name).write_text("untouched", encoding="utf-8")
        api._write_to_cache(newest, OBJ)
        self.assertFalse(oldest.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(newest.exists())
        self.assertEqual(unrelated.read_text(), "untouched")
        self.assertEqual((nested / oldest.name).read_text(), "untouched")
        oversized = api._get_cache_path("oversized", "obj")
        api._write_to_cache(oversized, OBJ * 3)
        self.assertFalse(oversized.exists())

    def test_prune_ignores_reparse_entries_without_following_them(self):
        path = self.api._get_cache_path("uuid", "obj")
        self.api._write_to_cache(path, OBJ)
        self.api.max_bytes = 0
        entry = SimpleNamespace(
            name=path.name,
            stat=Mock(
                return_value=SimpleNamespace(
                    st_mode=stat.S_IFREG,
                    st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
                )
            ),
        )
        with patch.object(
            cache.os, "scandir", return_value=contextlib.nullcontext(iter([entry]))
        ):
            self.api._prune()
        entry.stat.assert_called_once_with(follow_symlinks=False)
        self.assertEqual(path.read_text(encoding="utf-8"), OBJ)

    def test_prune_rechecks_links_before_deleting_scanned_files(self):
        path = self.api._get_cache_path("uuid", "obj")
        self.api._write_to_cache(path, OBJ)
        self.api.max_bytes = 0
        real_lstat = Path.lstat
        entry = SimpleNamespace(name=path.name, stat=Mock(return_value=path.lstat()))

        def lstat(candidate):
            if candidate == path:
                return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
            return real_lstat(candidate)

        with (
            patch.object(cache.os, "scandir", return_value=contextlib.nullcontext(iter([entry]))),
            patch.object(Path, "lstat", lstat),
        ):
            self.api._prune()
        self.assertEqual(path.read_text(encoding="utf-8"), OBJ)

    def test_cache_rejects_paths_outside_its_namespace(self):
        for path in (
            self.root / "user.obj",
            self.root.parent / self.api._get_cache_path("x", "obj").name,
        ):
            self.api._write_to_cache(path, OBJ)
            self.assertIsNone(self.api._read_from_cache(path))
            self.assertFalse(path.exists())

    def test_reparse_point_files_and_directories_are_rejected(self):
        path = self.api._get_cache_path("uuid", "obj")
        self.api._write_to_cache(path, OBJ)
        real_lstat = Path.lstat
        attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        for rejected in (self.root, path):

            def lstat(candidate):
                if candidate == rejected:
                    return SimpleNamespace(
                        st_mode=real_lstat(candidate).st_mode, st_file_attributes=attribute
                    )
                return real_lstat(candidate)

            with patch.object(Path, "lstat", lstat):
                self.assertIsNone(self.api._read_from_cache(path))
                self.api._write_to_cache(path, OBJ + "# must not write\n")
                self.api._prune()
            self.assertEqual(path.read_text(encoding="utf-8"), OBJ)

    def test_symlink_ancestor_is_rejected_even_if_cache_directory_is_not_created(self):
        real_lstat = Path.lstat

        def lstat(candidate):
            if candidate == self.root.parent:
                return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
            return real_lstat(candidate)

        path = self.api._get_cache_path("uuid", "obj")
        with patch.object(Path, "lstat", lstat):
            self.api._write_to_cache(path, OBJ)
            self.assertIsNone(self.api._read_from_cache(path))
        self.assertFalse(self.root.exists())

    def test_symlink_ancestor_is_not_followed(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        link = Path(self.temporary.name) / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks is not permitted on this host")
        api = cache.CachedEasyedaApi(cache_dir=link / "cache")
        path = api._get_cache_path("uuid", "obj")
        api._write_to_cache(path, OBJ)
        self.assertIsNone(api._read_from_cache(path))
        self.assertEqual(list(outside.iterdir()), [])

    def test_concurrent_writers_and_readers_only_observe_complete_payloads(self):
        variants = [OBJ + f"# variant {index}\n" * 200 for index in range(8)]
        shared = self.api._get_cache_path("shared", "obj")
        self.api._write_to_cache(shared, variants[0])
        start = threading.Barrier(9)

        def writer(index):
            start.wait()
            for _ in range(8):
                self.api._write_to_cache(shared, variants[index])
                path = self.api._get_cache_path(f"model-{index}", "step")
                self.api._write_to_cache(path, STEP, binary=True)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(writer, index) for index in range(8)]
            start.wait()
            while not all(future.done() for future in futures):
                observed = self.api._read_from_cache(shared)
                # A Windows sharing race may give a miss, but never a partial hit.
                self.assertTrue(observed is None or observed in variants)
            for future in futures:
                future.result()
        self.assertIn(self.api._read_from_cache(shared), variants)
        for index in range(8):
            self.assertEqual(
                self.api._read_from_cache(
                    self.api._get_cache_path(f"model-{index}", "step"), binary=True
                ),
                STEP,
            )
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_print_callback_failure_does_not_fail_hit(self):
        path = self.api._get_cache_path("uuid", "obj")
        self.api._write_to_cache(path, OBJ)
        self.api.print_func = Mock(side_effect=RuntimeError("UI already closed"))
        self.assertEqual(self.api._read_from_cache(path), OBJ)

    def test_default_location_is_stable_outside_project(self):
        with (
            patch.object(cache.sys, "platform", "win32"),
            patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}),
        ):
            self.assertEqual(cache.default_cache_dir(), self.root / "LCSCKiCadBridge/cache/v1")
        with (
            patch.object(cache.sys, "platform", "linux"),
            patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root)}),
        ):
            self.assertEqual(cache.default_cache_dir(), self.root / "LCSCKiCadBridge/cache/v1")
        with (
            patch.object(cache.sys, "platform", "linux"),
            patch.dict(os.environ, {"XDG_CACHE_HOME": "relative-project-folder"}),
        ):
            self.assertEqual(
                cache.default_cache_dir(), Path.home() / ".cache/LCSCKiCadBridge/cache/v1"
            )
        with patch.object(cache.sys, "platform", "darwin"):
            self.assertEqual(
                cache.default_cache_dir(), Path.home() / "Library/Caches/LCSCKiCadBridge/cache/v1"
            )


if __name__ == "__main__":
    unittest.main()
