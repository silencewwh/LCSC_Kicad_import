"""Bounded STEP range downloads with strict resource and interval validation."""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from ssl import SSLContext

PROBE_BYTES = 256 * 1024
PARALLEL_THRESHOLD = 1024 * 1024
MAX_WORKERS = 4
MAX_STEP_BYTES = 256 * 1024 * 1024
_CONTENT_RANGE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)\Z")


class _RangeFailure(ValueError):
    """A response cannot safely be combined with the other ranges."""


@dataclass(frozen=True)
class _Response:
    data: bytes
    total: int
    validator: tuple[str, str] | None = None
    complete: bool = False


def is_step_data(data: bytes) -> bool:
    if not isinstance(data, bytes) or not data or len(data) > MAX_STEP_BYTES:
        return False
    # Locate the trimmed boundaries without copying an entire (possibly large) model.
    start, end = 0, len(data)
    while start < end and data[start] in b" \t\n\r\v\f":
        start += 1
    while end > start and data[end - 1] in b" \t\n\r\v\f":
        end -= 1
    return (
        data.startswith(b"ISO-10303-21;", start, end)
        and data.endswith(b"END-ISO-10303-21;", start, end)
        and b"HEADER;" in data
        and b"DATA;" in data
        and b"ENDSEC;" in data
    )


def _header(response, name: str) -> str:
    headers = getattr(response, "headers", None)
    return str(headers.get(name, "")).strip() if headers is not None else ""


def _validator(response) -> tuple[str, str] | None:
    etag = _header(response, "ETag")
    if len(etag) >= 2 and etag.startswith('"') and etag.endswith('"'):
        return "ETag", etag
    modified = _header(response, "Last-Modified")
    if modified:
        try:
            parsed = parsedate_to_datetime(modified)
            if parsed.tzinfo is not None:
                return "Last-Modified", modified
        except (TypeError, ValueError, OverflowError):
            pass
    return None


def _read_response(response, start=None, end=None, total=None, validator=None) -> _Response:
    if response.status == 200:
        length = _header(response, "Content-Length")
        if length and (not length.isdecimal() or int(length) > MAX_STEP_BYTES):
            raise _RangeFailure("Invalid full STEP size")
        data = response.read(MAX_STEP_BYTES + 1)
        if not is_step_data(data) or (length and len(data) != int(length)):
            raise _RangeFailure("Invalid or incomplete full STEP response")
        # A server ignoring Range (or returning a newer resource after If-Range)
        # has already sent a whole file. Reuse it, never fetch it a second time.
        return _Response(data, len(data), complete=True)
    if response.status != 206 or start is None:
        raise _RangeFailure("Unexpected STEP response status")
    encoding = _header(response, "Content-Encoding").lower()
    if encoding not in ("", "identity"):
        raise _RangeFailure("Encoded range offsets cannot be safely combined")
    match = _CONTENT_RANGE.fullmatch(_header(response, "Content-Range"))
    if not match:
        raise _RangeFailure("Missing or invalid STEP Content-Range")
    received_start, received_end, received_total = map(int, match.groups())
    if (
        not 0 < received_total <= MAX_STEP_BYTES
        or received_start != start
        or received_end != min(end, received_total - 1)
        or received_end < received_start
        or (total is not None and received_total != total)
    ):
        raise _RangeFailure("STEP interval or total size changed")
    if validator is not None and _header(response, validator[0]) != validator[1]:
        raise _RangeFailure("STEP resource version changed")
    expected_size = received_end - received_start + 1
    length = _header(response, "Content-Length")
    if length and (not length.isdecimal() or int(length) != expected_size):
        raise _RangeFailure("STEP range length disagrees with its interval")
    data = response.read(expected_size + 1)
    if not isinstance(data, bytes) or len(data) != expected_size:
        raise _RangeFailure("Incomplete STEP range")
    return _Response(data, received_total, _validator(response))


def download_step_model(
    url: str,
    *,
    headers: Mapping[str, str],
    ssl_context: SSLContext | None,
    print_func: Callable[[str], None] | None = None,
) -> bytes:
    """Use at most four range workers and at most one ordinary GET fallback.

    Network failures propagate to the caller's existing optional-model handling.
    A complete 200 response is reused, including when a range worker receives it.
    """

    def notify(message):
        if print_func:
            try:
                print_func(message)
            except Exception:
                pass

    def request(start=None, end=None, total=None, validator=None):
        request_headers = {**headers, "Accept-Encoding": "identity"}
        if start is not None:
            request_headers["Range"] = f"bytes={start}-{end}"
        if validator is not None:
            request_headers["If-Range"] = validator[1]
        req = urllib.request.Request(url=url, headers=request_headers)
        with urllib.request.urlopen(req, timeout=30, context=ssl_context) as response:
            return _read_response(response, start, end, total, validator)

    def full_download():
        notify("STEP 模型分段响应无法安全合并，改为完整下载。")
        return request().data

    try:
        probe = request(0, PROBE_BYTES - 1)
    except urllib.error.HTTPError as error:
        if error.code in (400, 405, 416):
            return full_download()
        raise
    except (_RangeFailure, HTTPException):
        return full_download()
    if probe.complete:
        return probe.data
    if len(probe.data) == probe.total:
        return probe.data if is_step_data(probe.data) else full_download()
    if probe.validator is None:
        return full_download()

    remaining = probe.total - len(probe.data)
    workers = MAX_WORKERS if probe.total >= PARALLEL_THRESHOLD else 1
    chunk_size = (remaining + workers - 1) // workers
    intervals = [
        (start, min(start + chunk_size - 1, probe.total - 1))
        for start in range(len(probe.data), probe.total, chunk_size)
    ]
    pieces = {}
    whole_response = None
    failed = False
    denied_error = None
    if workers > 1:
        notify(f"STEP 模型分为 {len(intervals)} 段并行下载。")
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lcsc-step") as executor:
        futures = {
            executor.submit(request, start, end, probe.total, probe.validator): start
            for start, end in intervals
        }
        for future in as_completed(futures):
            try:
                part = future.result()
                if part.complete:
                    whole_response = part.data
                else:
                    pieces[futures[future]] = part.data
            except urllib.error.HTTPError as error:
                if error.code in (401, 403, 429):
                    denied_error = error
                else:
                    failed = True
            except (OSError, ValueError, HTTPException):
                failed = True
    if whole_response is not None:
        return whole_response
    if denied_error is not None:
        raise denied_error
    if failed:
        return full_download()
    data = b"".join([probe.data, *(pieces[start] for start, _ in intervals)])
    if len(data) != probe.total or not is_step_data(data):
        return full_download()
    return data
