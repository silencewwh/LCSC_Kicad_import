"""Bounded user cache for raw EasyEDA downloads, independent of project libraries."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from http.client import HTTPException
from pathlib import Path

from easyeda2kicad.easyeda.easyeda_api import ENDPOINT_3D_MODEL_STEP, EasyedaApi
from lcsc_model_download import download_step_model, is_step_data

CAD_TTL = 24 * 60 * 60
MODEL_TTL = 30 * 24 * 60 * 60
MAX_CACHE_BYTES = 256 * 1024 * 1024
_OWNED_NAME = re.compile(r"lcsc-v1-[0-9a-f]{64}\.(json|obj|step)\Z")
_CACHE_LOCK = threading.RLock()


def default_cache_dir() -> Path:
    if sys.platform == "win32":
        fallback = Path.home() / "AppData/Local"
        base = Path(os.environ.get("LOCALAPPDATA") or fallback)
    elif sys.platform == "darwin":
        fallback = base = Path.home() / "Library/Caches"
    else:
        fallback = Path.home() / ".cache"
        base = Path(os.environ.get("XDG_CACHE_HOME") or fallback)
    if not base.is_absolute():
        base = fallback
    return base / "LCSCKiCadBridge/cache/v1"


def _is_link(info: os.stat_result) -> bool:
    # Windows junctions are reparse points, but are not always reported as symlinks.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _valid_payload(extension: str, payload: bytes) -> bool:
    """Reject empty/error pages and truncated data without parsing entire CAD models."""
    if not payload:
        return False
    if extension == ".step":
        return is_step_data(payload)
    try:
        text = payload.decode("utf-8")
    except UnicodeError:
        return False
    if extension == ".json":
        try:
            response = json.loads(text)
        except (ValueError, RecursionError):
            return False
        if not isinstance(response, dict) or response.get("success") is False:
            return False
        result = response.get("result")
        data = result.get("dataStr") if isinstance(result, dict) else None
        return (
            isinstance(data, dict)
            and isinstance(data.get("head"), dict)
            and isinstance(data.get("shape"), list)
            and bool(data["shape"])
            and all(isinstance(shape, str) and shape for shape in data["shape"])
        )
    if extension == ".obj":
        has_vertex = has_face = False
        for line in text.splitlines():
            values = line.split()
            if len(values) >= 4 and values[0] == "v":
                try:
                    has_vertex = has_vertex or all(math.isfinite(float(v)) for v in values[1:4])
                except ValueError:
                    pass
            elif len(values) >= 4 and values[0] == "f":
                try:
                    has_face = has_face or all(int(v.split("/")[0]) != 0 for v in values[1:])
                except ValueError:
                    pass
            if has_vertex and has_face:
                return True
    return False


class CachedEasyedaApi(EasyedaApi):
    """Cache validated raw data and accelerate large STEP files with bounded ranges."""

    def __init__(
        self,
        use_cache: bool = True,
        *,
        cache_dir: Path | str | None = None,
        print_func: Callable[[str], None] | None = None,
        max_bytes: int = MAX_CACHE_BYTES,
    ) -> None:
        super().__init__(use_cache=use_cache)
        self.cache_dir = Path(os.path.abspath(cache_dir or default_cache_dir()))
        self.print_func = print_func
        self.max_bytes = max(0, int(max_bytes))

    def get_step_3d_model(self, uuid: str) -> bytes | None:
        path = self._get_cache_path(uuid, "step")
        cached = self._read_from_cache(path, binary=True)
        if isinstance(cached, bytes):
            return cached
        try:
            data = download_step_model(
                ENDPOINT_3D_MODEL_STEP.format(uuid=uuid),
                headers={"User-Agent": self.headers["User-Agent"]},
                ssl_context=self.ssl_context,
                print_func=self.print_func,
            )
        except (OSError, ValueError, HTTPException) as error:
            logging.error("STEP 模型下载失败：%s", type(error).__name__)
            return None
        self._write_to_cache(path, data, binary=True)
        return data

    def _get_cache_path(self, identifier: str, extension: str) -> Path:
        if extension not in ("json", "obj", "step"):
            raise ValueError("Unsupported download cache format")
        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
        return self.cache_dir / f"lcsc-v1-{digest}.{extension}"

    def _safe_directory(self, create: bool = False) -> bool:
        # Check every ancestor without resolving links into another location.
        for directory in (*reversed(self.cache_dir.parents), self.cache_dir):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                if not create:
                    return False
                try:
                    directory.mkdir()
                except FileExistsError:
                    pass  # A simultaneous writer may have created this directory.
                info = directory.lstat()
            if _is_link(info) or not stat.S_ISDIR(info.st_mode):
                return False
        return True

    def _owns_path(self, path: Path) -> bool:
        return path.parent == self.cache_dir and _OWNED_NAME.fullmatch(path.name) is not None

    def _read_from_cache(self, cache_path: Path, binary: bool = False) -> str | bytes | None:
        if not self.use_cache or not self._owns_path(cache_path):
            return None
        try:
            if not self._safe_directory():
                return None
            info = cache_path.lstat()
            ttl = CAD_TTL if cache_path.suffix == ".json" else MODEL_TTL
            if (
                _is_link(info)
                or not stat.S_ISREG(info.st_mode)
                or not 0 < info.st_size <= self.max_bytes
                or time.time() - info.st_mtime >= ttl
            ):
                return None
            descriptor = os.open(
                cache_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
            )
            with os.fdopen(descriptor, "rb") as stream:
                current = os.fstat(stream.fileno())
                if _is_link(current) or not stat.S_ISREG(current.st_mode):
                    return None
                payload = stream.read(self.max_bytes + 1)
            if len(payload) > self.max_bytes or not _valid_payload(cache_path.suffix, payload):
                return None
            result = payload if binary else payload.decode("utf-8")
        except (OSError, ValueError, UnicodeError):
            return None
        if self.print_func:
            kind = {".json": "器件数据", ".obj": "OBJ 模型", ".step": "STEP 模型"}[
                cache_path.suffix
            ]
            try:
                self.print_func(f"使用下载缓存：{kind}。")
            except Exception:
                pass
        return result

    def _write_to_cache(self, cache_path: Path, data: str | bytes, binary: bool = False) -> None:
        if not self.use_cache or not self._owns_path(cache_path):
            return
        temporary = None
        try:
            payload = data.encode("utf-8") if isinstance(data, str) else data
            if (
                not isinstance(payload, bytes)
                or len(payload) > self.max_bytes
                or not _valid_payload(cache_path.suffix, payload)
            ):
                return
            with _CACHE_LOCK:
                if not self._safe_directory(create=True):
                    return
                try:
                    info = cache_path.lstat()
                    if _is_link(info) or not stat.S_ISREG(info.st_mode):
                        return
                except FileNotFoundError:
                    pass
                descriptor, name = tempfile.mkstemp(
                    prefix=f".{cache_path.name}.", suffix=".tmp", dir=self.cache_dir
                )
                temporary = Path(name)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, cache_path)
                temporary = None
                self._prune()
        except (OSError, ValueError, UnicodeError):
            # Caching is optional. Filesystem failures must not fail a download.
            pass
        finally:
            if temporary is not None:
                try:
                    if not _is_link(temporary.lstat()):
                        temporary.unlink()
                except OSError:
                    pass

    def _prune(self) -> None:
        """Evict oldest owned files only; never follow links or walk subdirectories."""
        if not self._safe_directory():
            return
        entries = []
        with os.scandir(self.cache_dir) as children:
            for child in children:
                if not _OWNED_NAME.fullmatch(child.name):
                    continue
                try:
                    info = child.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and not _is_link(info):
                    entries.append((info.st_mtime, info.st_size, child.name))
        total = sum(size for _, size, _ in entries)
        if total <= self.max_bytes:
            return
        for _, size, name in sorted(entries):
            if total <= self.max_bytes:
                break
            path = self.cache_dir / name
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and not _is_link(info):
                    path.unlink()
                    total -= size
            except OSError:
                continue
