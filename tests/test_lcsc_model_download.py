"""Offline protocol checks for bounded, version-consistent STEP range downloads."""

import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
sys.path.insert(0, str(ROOT / "plugins/easyeda2kicad"))
import lcsc_download_cache as cache  # noqa: E402
import lcsc_model_download as download  # noqa: E402

ETAG = '"stable-step-version"'
MODIFIED = "Tue, 29 Sep 2026 10:00:00 GMT"
URL = "https://modules.example/model"


def step_payload(size, fill=b"x"):
    head = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n/*"
    tail = b"*/\nENDSEC;\nEND-ISO-10303-21;\n"
    return head + fill * (size - len(head) - len(tail)) + tail


class Response:
    def __init__(self, data, status=200, headers=None):
        self.data, self.status = data, status
        self.headers = headers if headers is not None else {"Content-Length": str(len(data))}
        self.closed = False
        self.position = 0
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def read(self, size=-1):
        self.read_sizes.append(size)
        end = len(self.data) if size < 0 else self.position + size
        data = self.data[self.position : end]
        self.position += len(data)
        return data


class StepDownloadTests(unittest.TestCase):
    def setUp(self):
        self.payload = step_payload(2 * 1024 * 1024 + 31)
        self.calls = []
        self.call_lock = threading.Lock()
        self.context = object()
        self.logs = []
        self.network = patch(
            "urllib.request.urlopen", side_effect=AssertionError("Network forbidden")
        )
        self.network.start()
        self.addCleanup(self.network.stop)

    def record(self, request, timeout, context):
        self.assertEqual(timeout, 30)
        self.assertIs(context, self.context)
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.assertEqual(headers["user-agent"], "original-agent")
        self.assertEqual(headers["accept-encoding"], "identity")
        with self.call_lock:
            self.calls.append(headers)
        return headers

    def serve(self, headers, payload=None, *, etag=ETAG, modified=None):
        payload = self.payload if payload is None else payload
        if "range" not in headers:
            return Response(payload)
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", headers["range"])
        start, end = map(int, match.groups())
        end = min(end, len(payload) - 1)
        response_headers = {
            "Content-Range": f"bytes {start}-{end}/{len(payload)}",
            "Content-Length": str(end - start + 1),
        }
        if etag is not None:
            response_headers["ETag"] = etag
        if modified is not None:
            response_headers["Last-Modified"] = modified
        return Response(payload[start : end + 1], 206, response_headers)

    def run_download(self, fetch):
        with patch("urllib.request.urlopen", side_effect=fetch):
            return download.download_step_model(
                URL,
                headers={"User-Agent": "original-agent"},
                ssl_context=self.context,
                print_func=self.logs.append,
            )

    def test_large_model_uses_four_concurrent_validated_ranges_in_correct_order(self):
        barrier = threading.Barrier(4)
        active = maximum = 0
        lock = threading.Lock()

        def fetch(request, timeout, context):
            nonlocal active, maximum
            headers = self.record(request, timeout, context)
            response = self.serve(headers)
            if "if-range" in headers:
                self.assertEqual(headers["if-range"], ETAG)
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                barrier.wait(timeout=5)
                start = int(headers["range"].split("=")[1].split("-")[0])
                time.sleep(0.003 * (4 - min(3, start // (len(self.payload) // 4))))
                with lock:
                    active -= 1
            return response

        self.assertEqual(self.run_download(fetch), self.payload)
        self.assertEqual(maximum, 4)
        self.assertEqual(len(self.calls), 5)
        self.assertTrue(all("range" in call for call in self.calls))
        self.assertIn("4 段并行", "".join(self.logs))

    def test_ignored_initial_range_reuses_single_complete_response(self):
        def fetch(request, timeout, context):
            self.record(request, timeout, context)
            return Response(self.payload)

        self.assertEqual(self.run_download(fetch), self.payload)
        self.assertEqual(len(self.calls), 1)

    def test_full_response_without_length_cannot_read_beyond_size_limit(self):
        for headers in ({}, {"Transfer-Encoding": "chunked"}):
            with self.subTest(headers=headers):
                response = Response(self.payload, headers=headers)
                with patch.object(download, "MAX_STEP_BYTES", 1024):
                    with self.assertRaises(ValueError):
                        download._read_response(response)
                self.assertEqual(response.read_sizes, [1025])
                self.assertEqual(response.position, 1025)

    def test_range_without_length_cannot_read_beyond_requested_interval(self):
        response = Response(
            self.payload, 206, {"Content-Range": f"bytes 0-15/{len(self.payload)}"}
        )
        with self.assertRaises(ValueError):
            download._read_response(response, 0, 15)
        self.assertEqual(response.read_sizes, [17])
        self.assertEqual(response.position, 17)

    def test_full_response_at_size_limit_is_accepted_and_larger_header_is_rejected(self):
        payload = step_payload(1024)
        with patch.object(download, "MAX_STEP_BYTES", len(payload)):
            response = Response(payload)
            self.assertEqual(download._read_response(response).data, payload)
            oversized = Response(payload, headers={"Content-Length": "1025"})
            with self.assertRaises(ValueError):
                download._read_response(oversized)
        self.assertEqual(oversized.read_sizes, [])

    def test_step_validation_preserves_whitespace_and_boundary_checks(self):
        payload = step_payload(128)
        self.assertTrue(download.is_step_data(b" \t\r\n\v\f" + payload + b" \t\r\n\v\f"))
        for invalid in (b" \t\r\n", b"x" + payload, payload + b"x", payload[:-20]):
            with self.subTest(invalid=invalid):
                self.assertFalse(download.is_step_data(invalid))

    def test_small_range_model_only_requests_one_remainder(self):
        self.payload = step_payload(700 * 1024)

        def fetch(request, timeout, context):
            return self.serve(self.record(request, timeout, context))

        self.assertEqual(self.run_download(fetch), self.payload)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(
            self.calls[1]["range"], f"bytes={download.PROBE_BYTES}-{len(self.payload) - 1}"
        )
        self.assertNotIn("并行", "".join(self.logs))

    def test_complete_small_probe_requires_no_additional_request(self):
        self.payload = step_payload(1024)

        def fetch(request, timeout, context):
            return self.serve(self.record(request, timeout, context), etag=None)

        self.assertEqual(self.run_download(fetch), self.payload)
        self.assertEqual(len(self.calls), 1)

    def test_server_rejecting_range_gets_one_ordinary_get(self):
        for status in (400, 405, 416):
            with self.subTest(status=status):
                self.calls = []

                def fetch(request, timeout, context):
                    headers = self.record(request, timeout, context)
                    if "range" in headers:
                        raise urllib.error.HTTPError(URL, status, "Range rejected", {}, None)
                    return Response(self.payload)

                self.assertEqual(self.run_download(fetch), self.payload)
                self.assertEqual(len(self.calls), 2)
                self.assertNotIn("range", self.calls[-1])

    def test_missing_or_weak_validator_uses_one_ordinary_get(self):
        for etag in (None, 'W/"weak-version"'):
            with self.subTest(etag=etag):
                self.calls = []

                def fetch(request, timeout, context):
                    return self.serve(self.record(request, timeout, context), etag=etag)

                self.assertEqual(self.run_download(fetch), self.payload)
                self.assertEqual(len(self.calls), 2)
                self.assertNotIn("range", self.calls[-1])
                self.assertNotIn("if-range", self.calls[-1])

    def test_last_modified_can_pin_resource_without_strong_etag(self):
        def fetch(request, timeout, context):
            headers = self.record(request, timeout, context)
            if "if-range" in headers:
                self.assertEqual(headers["if-range"], MODIFIED)
            return self.serve(headers, etag=None, modified=MODIFIED)

        self.assertEqual(self.run_download(fetch), self.payload)
        self.assertEqual(len(self.calls), 5)

    def test_worker_ignoring_range_returns_complete_file_without_another_get(self):
        newer = step_payload(len(self.payload) + 100, b"n")

        def fetch(request, timeout, context):
            headers = self.record(request, timeout, context)
            if headers.get("range", "").startswith(f"bytes={download.PROBE_BYTES}-"):
                return Response(newer)
            return self.serve(headers)

        self.assertEqual(self.run_download(fetch), newer)
        self.assertEqual(len(self.calls), 5)
        self.assertTrue(all("range" in call for call in self.calls))

    def test_invalid_ranges_or_changed_versions_are_discarded_for_full_get(self):
        for failure in (
            "range",
            "total",
            "etag",
            "missing-etag",
            "length",
            "truncated",
            "encoding",
        ):
            with self.subTest(failure=failure):
                self.calls = []

                def fetch(request, timeout, context):
                    headers = self.record(request, timeout, context)
                    response = self.serve(headers)
                    if headers.get("range", "").startswith(f"bytes={download.PROBE_BYTES}-"):
                        if failure == "range":
                            response.headers["Content-Range"] = f"bytes 0-100/{len(self.payload)}"
                        elif failure == "total":
                            response.headers["Content-Range"] = response.headers[
                                "Content-Range"
                            ].replace(f"/{len(self.payload)}", f"/{len(self.payload) + 1}")
                        elif failure == "etag":
                            response.headers["ETag"] = '"changed-version"'
                        elif failure == "missing-etag":
                            del response.headers["ETag"]
                        elif failure == "length":
                            response.headers["Content-Length"] = "1"
                        elif failure == "truncated":
                            response.data = response.data[:-1]
                        elif failure == "encoding":
                            response.headers["Content-Encoding"] = "gzip"
                    return response

                self.assertEqual(self.run_download(fetch), self.payload)
                self.assertEqual(sum("range" not in call for call in self.calls), 1)
                self.assertLessEqual(len(self.calls), 6)

    def test_worker_network_failure_has_only_one_full_download_fallback(self):
        def fetch(request, timeout, context):
            headers = self.record(request, timeout, context)
            if "if-range" in headers:
                raise urllib.error.URLError("fixture interrupted")
            return self.serve(headers)

        self.assertEqual(self.run_download(fetch), self.payload)
        self.assertEqual(len(self.calls), 6)
        self.assertNotIn("range", self.calls[-1])

    def test_incomplete_http_body_falls_back_once_and_never_uses_partial_bytes(self):
        for stage in ("probe", "worker"):
            with self.subTest(stage=stage):
                self.calls = []

                def fetch(request, timeout, context):
                    headers = self.record(request, timeout, context)
                    should_fail = "range" in headers and (stage == "probe" or "if-range" in headers)
                    if should_fail:
                        raise IncompleteRead(b"partial", 100)
                    return self.serve(headers)

                self.assertEqual(self.run_download(fetch), self.payload)
                self.assertEqual(sum("range" not in call for call in self.calls), 1)
                self.assertLessEqual(len(self.calls), 6)

    def test_permission_denied_is_not_retried(self):
        def fetch(request, timeout, context):
            self.record(request, timeout, context)
            raise urllib.error.HTTPError(URL, 403, "Denied", {}, None)

        with self.assertRaises(urllib.error.HTTPError):
            self.run_download(fetch)
        self.assertEqual(len(self.calls), 1)

    def test_worker_rate_limit_does_not_trigger_an_extra_full_request(self):
        def fetch(request, timeout, context):
            headers = self.record(request, timeout, context)
            if "if-range" in headers:
                raise urllib.error.HTTPError(URL, 429, "Rate limited", {}, None)
            return self.serve(headers)

        with self.assertRaises(urllib.error.HTTPError):
            self.run_download(fetch)
        self.assertEqual(len(self.calls), 5)
        self.assertTrue(all("range" in call for call in self.calls))

    def test_initial_network_failure_is_not_retried(self):
        def fetch(request, timeout, context):
            self.record(request, timeout, context)
            raise urllib.error.URLError("fixture offline")

        with self.assertRaises(urllib.error.URLError):
            self.run_download(fetch)
        self.assertEqual(len(self.calls), 1)

    def test_invalid_combined_step_is_never_returned_or_cached(self):
        invalid = b"not-a-step" + self.payload[10:]
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                cache.CachedEasyedaApi, "_create_ssl_context", return_value=self.context
            ):
                api = cache.CachedEasyedaApi(cache_dir=Path(directory))
            api.headers["User-Agent"] = "original-agent"

            def fetch(request, timeout, context):
                headers = self.record(request, timeout, context)
                return self.serve(headers, payload=invalid)

            with patch("urllib.request.urlopen", side_effect=fetch), self.assertLogs(level="ERROR"):
                self.assertIsNone(api.get_step_3d_model("uuid"))
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertEqual(sum("range" not in call for call in self.calls), 1)

    def test_cache_hit_never_starts_probe_or_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(cache.CachedEasyedaApi, "_create_ssl_context", return_value=None):
                api = cache.CachedEasyedaApi(cache_dir=Path(directory))
            api._write_to_cache(api._get_cache_path("uuid", "step"), self.payload, binary=True)
            with patch(
                "urllib.request.urlopen", side_effect=AssertionError("Cache must avoid network")
            ) as fetch:
                self.assertEqual(api.get_step_3d_model("uuid"), self.payload)
                fetch.assert_not_called()

    def test_full_response_without_headers_uses_bounded_read(self):
        class MinimalResponse:
            status = 200

            def read(inner, size):
                self.assertEqual(size, download.MAX_STEP_BYTES + 1)
                return self.payload[:size]

            def __enter__(inner):
                return inner

            def __exit__(inner, *args):
                return None

        def fetch(request, timeout, context):
            self.record(request, timeout, context)
            return MinimalResponse()

        self.assertEqual(self.run_download(fetch), self.payload)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
