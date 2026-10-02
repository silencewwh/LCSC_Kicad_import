"""Transactional EasyEDA imports into the current KiCad project only.

This module deliberately has no wx/pcbnew dependency so importing, registration,
and recovery can be exercised without starting KiCad.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_MANIFEST = ".lcsc-import.json"
_OWNER = "lcsc-project-import-v1"
_SHARED_OWNER = "lcsc-project-import-v2"
_LIBRARY = "LCSC"
_RECOVERY = ".import-recovery"


@dataclass(frozen=True)
class ImportResult:
    component_id: str
    library_name: str
    directory: Path
    already_present: bool
    has_3d: bool


def _inside(path: Path, root: Path) -> Path:
    """Reject symbolic links/junctions and paths outside a project-owned directory."""
    root = root.resolve()
    absolute = Path(os.path.abspath(path))
    if not absolute.is_relative_to(root):
        raise ValueError(f"Path is outside the import directory: {path}")
    current = absolute
    while current != root:
        if current.is_symlink() or current.resolve() != current:
            raise ValueError(f"Linked paths are not supported for imports: {current}")
        current = current.parent
    return absolute


def _safe_name(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
        or name.endswith((".", " "))
        or name.split(".")[0].upper()
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *[f"COM{i}" for i in range(10)],
            *[f"LPT{i}" for i in range(10)],
        }
    ):
        raise ValueError(f"CAD data contains an unsafe file name: {name!r}")


def _convert_component(
    component_id: str, directory: Path, library_name: str, print_func: Callable[[str], None]
) -> Any:
    # Import lazily: URL recognition and project checks work even when a bundled
    # converter is missing, and importing this module does not initialize KiCad.
    try:
        from . import impart_easyeda as converter
    except ImportError:
        import impart_easyeda as converter  # type: ignore[no-redef]

    class ProjectImporter(converter.EasyEDAImporter):
        def _import_footprint(self, footprint: Any, model_ext: str = "wrl") -> Path | None:
            _safe_name(footprint.info.name)
            if not getattr(self, "_has_downloaded_model", False):
                footprint.model_3d = None
            elif footprint.model_3d is not None:
                _safe_name(footprint.model_3d.name)
            return super()._import_footprint(footprint, model_ext)

        def _import_3d_model(self, cad_data: dict[str, Any]) -> tuple[Path | None, Path | None]:
            model = converter.Easyeda3dModelImporter(
                easyeda_cp_cad_data=cad_data, download_raw_3d_model=False, api=self.api
            ).output
            if model is not None:
                _safe_name(model.name)
            paths = tuple(
                path if path is not None and path.is_file() and path.stat().st_size > 0 else None
                for path in super()._import_3d_model(cad_data)
            )
            self._has_downloaded_model = any(path is not None for path in paths)
            return paths[0], paths[1]

    config = converter.ImportConfig(
        base_folder=directory,
        lib_name=library_name,
        overwrite=False,
        lib_var="${KIPRJMOD}/LCSC",
        prefer_step=True,
    )
    return ProjectImporter(config, print_func).import_component(component_id)


def _parse_sexpr(
    text: str, child_forms: list[tuple[list[Any], int, int]] | None = None
) -> tuple[list[Any], int]:
    """Parse tables and assets strictly, retaining the root's closing position.

    Unlike reserializing a library table, inserting before that position leaves
    comments, disabled entries and future KiCad fields byte-for-byte intact.
    """
    stack: list[list[Any]] = []
    root: list[Any] | None = None
    close_at = -1
    child_start = 0
    i = 0
    while i < len(text):
        char = text[i]
        if char.isspace() or (i == 0 and char == "\ufeff"):
            i += 1
            continue
        if char == ";":
            newline = text.find("\n", i)
            i = len(text) if newline < 0 else newline + 1
            continue
        if char == "(":
            node: list[Any] = []
            if len(stack) == 1:
                child_start = i
            if stack:
                stack[-1].append(node)
            elif root is not None:
                raise ValueError("Multiple S-expression roots")
            else:
                root = node
            stack.append(node)
            i += 1
            continue
        if char == ")":
            if not stack:
                raise ValueError("Unexpected closing parenthesis")
            node = stack.pop()
            if len(stack) == 1 and child_forms is not None:
                child_forms.append((node, child_start, i + 1))
            if not stack:
                close_at = i
            i += 1
            continue
        if not stack:
            raise ValueError("Text outside the S-expression root")
        if char == '"':
            value: list[str] = []
            i += 1
            while i < len(text) and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                    if i >= len(text):
                        raise ValueError("Unterminated escape in quoted string")
                value.append(text[i])
                i += 1
            if i >= len(text):
                raise ValueError("Unterminated quoted string")
            stack[-1].append("".join(value))
            i += 1
        else:
            end = i
            while end < len(text) and not text[end].isspace() and text[end] not in '();"':
                end += 1
            if end == i:
                raise ValueError("Invalid S-expression token")
            stack[-1].append(text[i:end])
            i = end
    if stack or root is None or close_at < 0:
        raise ValueError("Incomplete S-expression")
    return root, close_at


def _table_update(original: bytes | None, kind: str, nickname: str, uri: str) -> bytes:
    text = original.decode("utf-8") if original is not None else f"({kind}\n)\n"
    root, closing = _parse_sexpr(text)
    if not root or root[0] != kind:
        raise ValueError(f"Invalid {kind}: unexpected table type")
    matches = []
    for node in root[1:]:
        if not isinstance(node, list) or not node or node[0] != "lib":
            continue
        fields = {field[0]: field[1:] for field in node[1:] if isinstance(field, list) and field}
        if fields.get("name") == [nickname]:
            matches.append(fields)
    if matches:
        if len(matches) != 1 or matches[0].get("uri") != [uri]:
            raise ValueError(f"Library name {nickname} already points to another library in {kind}")
        if matches[0].get("type") != ["KiCad"] or "disabled" in matches[0]:
            raise ValueError(f"Library {nickname} is disabled or is not a KiCad library in {kind}")
        return original if original is not None else text.encode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    description = (
        "LCSC project components"
        if nickname == _LIBRARY
        else f"LCSC {nickname.removeprefix('LCSC_')}"
    )
    entry = (
        f'  (lib (name "{nickname}")(type "KiCad")(uri "{uri}")'
        f'(options "")(descr "{description}")){newline}'
    )
    separator = "" if closing > 0 and text[closing - 1] == "\n" else newline
    return (text[:closing] + separator + entry + text[closing:]).encode("utf-8")


def _read_optional(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, filename = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _project_lock(path: Path):
    # Keep the inode/file in place: deleting it after unlocking lets another process
    # hold an old lock while a third process creates and locks a different file.
    with path.open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError(f"Another project import is running (lock: {path}).") from error
        # Closing the handle releases the OS lock, including after an exception.
        yield


def _recovery_path(project: Path, relative: str) -> Path:
    path = _inside(project / relative, project)
    if path in {project / "sym-lib-table", project / "fp-lib-table"}:
        return path
    directory = project / _LIBRARY
    if path in {directory / _MANIFEST, directory / "LCSC.kicad_sym"}:
        return path
    if path.parent in {directory / "LCSC.pretty", directory / "LCSC.3dshapes"}:
        _safe_name(path.name)
        if re.match(r"C[1-9][0-9]*_", path.name):
            return path
    raise ValueError(f"Unexpected path in import recovery record: {relative}")


def _remove_recovery(recovery: Path) -> None:
    for path in recovery.rglob("*"):
        _inside(path, recovery)
    # Retain the completion marker until all backups are gone, so interrupted
    # cleanup never makes a completed transaction look like an incomplete one.
    for path in recovery.iterdir():
        if path.name != "committed":
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    (recovery / "committed").unlink(missing_ok=True)
    recovery.rmdir()


def _recover_import(project: Path, directory: Path) -> None:
    """Undo an interrupted import only while every file still matches our journal."""
    # These journals were never published, so no live asset could have been changed.
    for pending in directory.glob(".journal-*"):
        _inside(pending, directory)
        marker = _inside(pending / "owner", directory)
        if marker.is_file() and marker.read_bytes() == _SHARED_OWNER.encode("ascii"):
            _remove_recovery(pending)
    recovery = _inside(directory / _RECOVERY, project)
    if not recovery.exists():
        return
    if not any(recovery.iterdir()):
        recovery.rmdir()
        return
    committed = _inside(recovery / "committed", recovery)
    if committed.is_file() and committed.read_bytes() == b"committed\n":
        _remove_recovery(recovery)
        return
    try:
        journal = json.loads(_inside(recovery / "journal.json", recovery).read_bytes())
        if journal["owner"] != _SHARED_OWNER or not isinstance(journal["files"], list):
            raise ValueError("invalid recovery record")
        restore = []
        seen = set()
        for index, entry in enumerate(journal["files"]):
            path = _recovery_path(project, entry["path"])
            if path in seen:
                raise ValueError("duplicate recovery path")
            seen.add(path)
            before = entry["before"]
            after = entry["after"]
            for digest in (before, after):
                if digest is not None and (
                    not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                ):
                    raise ValueError("invalid recovery digest")
            backup = (
                _inside(recovery / str(index), recovery).read_bytes()
                if before is not None
                else None
            )
            if backup is not None and _digest(backup) != before:
                raise ValueError("damaged recovery backup")
            current = _read_optional(path)
            current_digest = _digest(current) if current is not None else None
            if current_digest not in (before, after):
                raise FileExistsError(f"File was edited after interrupted import: {path}")
            if current_digest != before:
                restore.append((path, backup))
        # Check the entire journal before restoring anything; never erase later user edits.
        for path, backup in reversed(restore):
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, backup)
        for parent in (directory / "LCSC.pretty", directory / "LCSC.3dshapes"):
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        _atomic_write(recovery / "committed", b"committed\n")
        _remove_recovery(recovery)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(
            f"Interrupted import needs attention; recovery preserved at {recovery}: {error}"
        ) from error


def _commit_import(project: Path, directory: Path, updates: dict, originals: dict) -> None:
    changed = [(path, after) for path, after in updates.items() if originals[path] != after]
    if not changed:
        return
    # A complete journal becomes visible before any library file is changed.
    temporary = Path(tempfile.mkdtemp(prefix=".journal-", dir=directory))
    recovery = directory / _RECOVERY
    try:
        _atomic_write(temporary / "owner", _SHARED_OWNER.encode("ascii"))
        entries = []
        for index, (path, after) in enumerate(changed):
            before = originals[path]
            if before is not None:
                _atomic_write(temporary / str(index), before)
            entries.append(
                {
                    "path": path.relative_to(project).as_posix(),
                    "before": _digest(before) if before is not None else None,
                    "after": _digest(after) if after is not None else None,
                }
            )
        _atomic_write(
            temporary / "journal.json",
            json.dumps({"owner": _SHARED_OWNER, "files": entries}).encode("utf-8"),
        )
        os.replace(temporary, recovery)
    finally:
        if temporary.exists():
            _remove_recovery(temporary)
    transaction = uuid.uuid4().hex
    try:
        # Complete assets before advertising the library to KiCad.
        for path, after in sorted(changed, key=lambda item: item[0].parent == project):
            before = originals[path]
            if before is not None and path.parent == project:
                with path.with_name(f"{path.name}.lcsc-{transaction}.bak").open("xb") as stream:
                    stream.write(before)
            path.parent.mkdir(exist_ok=True)
            if after is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, after)
        _atomic_write(recovery / "committed", b"committed\n")
    except Exception:
        _recover_import(project, directory)
        raise
    _remove_recovery(recovery)


def _validate_assets(directory: Path, library_name: str, paths: Any) -> dict[str, Any]:
    for asset in directory.rglob("*"):
        _inside(asset, directory)
    symbol_path = getattr(paths, "symbol_lib", None)
    footprint_path = getattr(paths, "footprint_file", None)
    if symbol_path is None or footprint_path is None:
        raise RuntimeError(
            "Import did not create both a symbol and a footprint; nothing was installed."
        )
    symbol = _inside(Path(symbol_path), directory)
    footprint = _inside(Path(footprint_path), directory)
    if symbol != directory / f"{library_name}.kicad_sym":
        raise RuntimeError("Converter returned an unexpected symbol library path")
    if footprint.parent != directory / f"{library_name}.pretty" or footprint.suffix != ".kicad_mod":
        raise RuntimeError("Converter returned an unexpected footprint path")
    symbol_tree, _ = _parse_sexpr(symbol.read_text(encoding="utf-8"))
    footprint_tree, _ = _parse_sexpr(footprint.read_text(encoding="utf-8"))
    if (
        not symbol_tree
        or symbol_tree[0] != "kicad_symbol_lib"
        or not any(
            isinstance(item, list) and item and item[0] == "symbol" for item in symbol_tree[1:]
        )
    ):
        raise RuntimeError("Converter did not produce a valid symbol library")
    if not footprint_tree or footprint_tree[0] not in {"footprint", "module"}:
        raise RuntimeError("Converter did not produce a valid footprint")
    models = []
    for attr in ("model_wrl", "model_step"):
        value = getattr(paths, attr, None)
        if value is not None:
            path = _inside(Path(value), directory)
            if path.parent != directory / f"{library_name}.3dshapes" or not path.is_file():
                raise RuntimeError("Converter returned an unexpected 3D model path")
            if path.stat().st_size:
                models.append(path.relative_to(directory).as_posix())
    files = sorted(
        path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()
    )
    return {
        "owner": _OWNER,
        "library_name": library_name,
        "symbol": symbol.relative_to(directory).as_posix(),
        "footprint": footprint.relative_to(directory).as_posix(),
        "models": models,
        "files": files,
    }


def _owned_manifest(directory: Path, component_id: str, library_name: str) -> dict[str, Any] | None:
    if not directory.exists():
        return None
    manifest_path = _inside(directory / _MANIFEST, directory)
    if not manifest_path.is_file():
        raise FileExistsError(f"Import folder already contains unowned content: {directory}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest["owner"] != _OWNER
            or manifest["component_id"] != component_id
            or manifest["library_name"] != library_name
        ):
            raise ValueError("ownership mismatch")
        if (
            not isinstance(manifest["files"], list)
            or not isinstance(manifest["models"], list)
            or manifest["symbol"] != f"{library_name}.kicad_sym"
            or Path(manifest["footprint"]).parent != Path(f"{library_name}.pretty")
            or Path(manifest["footprint"]).suffix != ".kicad_mod"
        ):
            raise ValueError("invalid asset list")
        for relative in [
            *manifest["files"],
            manifest["symbol"],
            manifest["footprint"],
            *manifest["models"],
        ]:
            if not isinstance(relative, str):
                raise ValueError("invalid asset path")
            _inside(directory / relative, directory)
        if not {manifest["symbol"], manifest["footprint"], *manifest["models"]}.issubset(
            manifest["files"]
        ):
            raise ValueError("incomplete ownership record")
        return manifest
    except (ValueError, KeyError, TypeError) as error:
        raise FileExistsError(
            f"Import folder has an invalid ownership record: {directory}"
        ) from error


def _complete(directory: Path, manifest: dict[str, Any]) -> bool:
    try:
        symbol, _ = _parse_sexpr((directory / manifest["symbol"]).read_text(encoding="utf-8"))
        footprint, _ = _parse_sexpr((directory / manifest["footprint"]).read_text(encoding="utf-8"))
        return bool(
            symbol
            and symbol[0] == "kicad_symbol_lib"
            and any(isinstance(item, list) and item and item[0] == "symbol" for item in symbol[1:])
            and footprint
            and footprint[0] in {"footprint", "module"}
            and all(
                (directory / relative).is_file() and (directory / relative).stat().st_size > 0
                for relative in manifest["models"]
            )
        )
    except (OSError, ValueError):
        return False


def _forms(text: str) -> list[tuple[list[Any], int, int]]:
    """Locate root child forms without rewriting their contents or comments."""
    forms = []
    _parse_sexpr(text, forms)
    return forms


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _rewrite_asset(
    text: str, component_id: str, footprint_name: str, models: dict[str, str]
) -> str:
    """Rename CAD identifiers while retaining unknown fields, layout and comments."""
    _parse_sexpr(text)
    stack: list[list[str]] = []
    replacements = []
    for token in re.finditer(r'"(?:\\.|[^"\\])*"|;[^\r\n]*|[()]|[^()\s";]+', text):
        raw = token.group()
        if raw.startswith(";") or (token.start() == 0 and raw == "\ufeff"):
            continue
        if raw == "(":
            stack.append([])
            continue
        if raw == ")":
            stack.pop()
            continue
        value = _parse_sexpr("(" + raw + ")")[0][0]
        form = stack[-1]
        replacement = None
        if len(form) == 1 and form[0] in {"symbol", "extends"}:
            replacement = component_id + "_" + value
        elif len(form) == 1 and form[0] in {"footprint", "module"}:
            replacement = footprint_name
        elif len(form) == 2 and form == ["property", "Footprint"]:
            replacement = _LIBRARY + ":" + footprint_name
        elif len(form) == 1 and form[0] == "model":
            basename = value.replace("\\", "/").rsplit("/", 1)[-1]
            if basename not in models:
                raise ValueError(f"Footprint refers to an unavailable 3D model: {basename}")
            replacement = "${KIPRJMOD}/LCSC/LCSC.3dshapes/" + models[basename]
        if replacement is not None:
            replacements.append((token.start(), token.end(), _quote(replacement)))
        form.append(value)
    for start, end, replacement in reversed(replacements):
        text = text[:start] + replacement + text[end:]
    return text


def _symbol_forms(text: str) -> dict[str, tuple[str, int, int]]:
    forms = []
    root, _ = _parse_sexpr(text, forms)
    if not root or root[0] != "kicad_symbol_lib":
        raise ValueError("LCSC.kicad_sym is not a KiCad symbol library")
    symbols: dict[str, tuple[str, int, int]] = {}
    for form, start, end in forms:
        if form and form[0] == "symbol":
            if len(form) < 2 or not isinstance(form[1], str) or form[1] in symbols:
                raise ValueError("Invalid or duplicate symbol in LCSC.kicad_sym")
            symbols[form[1]] = (text[start:end], start, end)
    return symbols


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _shared_manifest(directory: Path) -> dict[str, Any]:
    path = _inside(directory / _MANIFEST, directory)
    if not path.exists():
        return {"owner": _SHARED_OWNER, "library_name": _LIBRARY, "components": {}}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if (
            manifest["owner"] != _SHARED_OWNER
            or manifest["library_name"] != _LIBRARY
            or not isinstance(manifest["components"], dict)
        ):
            raise ValueError("ownership mismatch")
        all_files: set[str] = set()
        all_symbols: set[str] = set()
        for component, record in manifest["components"].items():
            if re.fullmatch(r"C[1-9][0-9]*", component) is None:
                raise ValueError("invalid component ID")
            if not isinstance(record["files"], dict) or not isinstance(record["symbols"], dict):
                raise ValueError("invalid ownership record")
            if not record["symbols"] or not isinstance(record["models"], list):
                raise ValueError("incomplete ownership record")
            if record.get("model_status") not in {None, "available", "unavailable", "failed"}:
                raise ValueError("invalid model status")
            if record["footprint"] not in record["files"]:
                raise ValueError("missing footprint ownership")
            if not set(record["models"]).issubset(record["files"]):
                raise ValueError("missing model ownership")
            for relative, digest in record["files"].items():
                asset = _inside(directory / relative, directory)
                if (
                    asset.parent not in {directory / "LCSC.pretty", directory / "LCSC.3dshapes"}
                    or not asset.name.startswith(component + "_")
                    or relative.casefold() in all_files
                    or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                ):
                    raise ValueError("invalid owned asset")
                all_files.add(relative.casefold())
            for name, digest in record["symbols"].items():
                if (
                    not name.startswith(component + "_")
                    or name in all_symbols
                    or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                ):
                    raise ValueError("invalid owned symbol")
                all_symbols.add(name)
        return manifest
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise FileExistsError(f"Invalid LCSC ownership record: {path}") from error


def _component_complete(directory: Path, record: dict[str, Any], symbols: dict[str, Any]) -> bool:
    # Existing user edits remain usable, and a repeated click never replaces them.
    if not set(record["symbols"]).issubset(symbols):
        return False
    try:
        footprint, _ = _parse_sexpr((directory / record["footprint"]).read_text(encoding="utf-8"))
        return (
            all(
                _inside(directory / relative, directory).is_file()
                and (directory / relative).stat().st_size > 0
                for relative in record["files"]
            )
            and bool(footprint)
            and footprint[0]
            in {
                "footprint",
                "module",
            }
        )
    except (OSError, ValueError):
        return False


def _legacy_copy(project: Path, component_id: str, staged: Path) -> Any | None:
    """Reuse complete old imports locally; keep their original files and lib IDs."""
    legacy = _inside(project / "lcsc" / component_id, project)
    if not (legacy / _MANIFEST).is_file():
        return None
    name = "LCSC_" + component_id
    manifest = _owned_manifest(legacy, component_id, name)
    if manifest is None or not _complete(legacy, manifest):
        return None
    destinations = {}
    for relative in [manifest["symbol"], manifest["footprint"], *manifest["models"]]:
        source = _inside(legacy / relative, legacy)
        if relative == manifest["symbol"]:
            target = staged / "LCSC.kicad_sym"
            attr = "symbol_lib"
        elif relative == manifest["footprint"]:
            target = staged / "LCSC.pretty" / source.name
            attr = "footprint_file"
        else:
            target = staged / "LCSC.3dshapes" / source.name
            attr = "model_wrl" if source.suffix == ".wrl" else "model_step"
        target.parent.mkdir(exist_ok=True)
        shutil.copy2(source, target)
        destinations[attr] = target
    return SimpleNamespace(**{"model_wrl": None, "model_step": None, **destinations})


def _prepare_component(component_id: str, staged: Path, paths: Any) -> tuple[dict, dict, dict]:
    assets = _validate_assets(staged, _LIBRARY, paths)
    footprint_path = staged / assets["footprint"]
    _safe_name(footprint_path.stem)
    footprint_name = component_id + "_" + footprint_path.stem
    model_names = {}
    files = {}
    for relative in assets["models"]:
        path = staged / relative
        _safe_name(path.name)
        name = component_id + "_" + path.name
        model_names[path.name] = name
        files["LCSC.3dshapes/" + name] = path.read_bytes()
    footprint_relative = "LCSC.pretty/" + footprint_name + ".kicad_mod"
    files[footprint_relative] = _rewrite_asset(
        footprint_path.read_text(encoding="utf-8"), component_id, footprint_name, model_names
    ).encode("utf-8")
    symbol_text = _rewrite_asset(
        (staged / assets["symbol"]).read_text(encoding="utf-8"),
        component_id,
        footprint_name,
        model_names,
    )
    symbols = {name: form[0] for name, form in _symbol_forms(symbol_text).items()}
    record = {
        "symbols": {name: _digest(text.encode("utf-8")) for name, text in symbols.items()},
        "files": {relative: _digest(content) for relative, content in files.items()},
        "footprint": footprint_relative,
        "models": sorted(relative for relative in files if relative.startswith("LCSC.3dshapes/")),
        "model_status": (
            "available" if assets["models"] else getattr(paths, "model_status", "unavailable")
        ),
    }
    return record, symbols, files


def _merge_retried_models(directory: Path, record: dict, incoming: dict, files: dict) -> tuple:
    if not incoming["models"]:
        return {**record, "model_status": incoming["model_status"]}, {}
    if incoming["footprint"] != record["footprint"]:
        raise FileExistsError("Downloaded footprint name changed; cannot attach its 3D model safely")
    footprint = directory / record["footprint"]
    before = footprint.read_bytes()
    if _digest(before) != record["files"][record["footprint"]]:
        raise FileExistsError(f"Imported footprint was edited; refusing to overwrite: {footprint}")
    # Keep the existing geometry and symbol. Only attach the newly downloaded models.
    converted = files[incoming["footprint"]].decode("utf-8")
    models = [
        converted[start:end] for form, start, end in _forms(converted) if form and form[0] == "model"
    ]
    if not models:
        raise ValueError("Downloaded 3D model has no footprint reference")
    original = before.decode("utf-8")
    _, closing = _parse_sexpr(original)
    addition = "\n" + "\n".join("  " + model for model in models) + "\n"
    updates = {relative: files[relative] for relative in incoming["models"]}
    updates[record["footprint"]] = (original[:closing] + addition + original[closing:]).encode("utf-8")
    return {
        **record,
        "files": {**record["files"], **{path: _digest(data) for path, data in updates.items()}},
        "models": incoming["models"],
        "model_status": "available",
    }, updates


def _merge_symbols(
    original: str, incoming: dict[str, str], owned: dict[str, str], existing: dict | None = None
) -> str:
    if existing is None:
        existing = _symbol_forms(original)
    for name, (_, start, end) in existing.items():
        if name in owned and _digest(original[start:end].encode("utf-8")) != owned[name]:
            raise FileExistsError(f"Imported symbol was edited; refusing to overwrite: {name}")
        if name in incoming and name not in owned:
            raise FileExistsError(f"Symbol already exists in LCSC: {name}")
    # Replace only forms owned by this component; leave surrounding comments intact.
    for name, (_, start, end) in sorted(
        existing.items(), key=lambda item: item[1][1], reverse=True
    ):
        if name in owned:
            original = original[:start] + original[end:]
    _, closing = _parse_sexpr(original)
    newline = "\r\n" if "\r\n" in original else "\n"
    addition = newline + newline.join("  " + form for form in incoming.values()) + newline
    return original[:closing] + addition + original[closing:]


def import_component_to_project(
    component_id: str,
    project_dir: Path,
    print_func: Callable[[str], None] = print,
    overwrite: bool = False,
) -> ImportResult:
    """Merge one component into the project's shared LCSC library transactionally."""
    component_id = component_id.strip().upper()
    if re.fullmatch(r"C[1-9][0-9]*", component_id) is None:
        raise ValueError("Invalid LCSC component ID; expected C2040, for example.")
    project = Path(project_dir).expanduser().resolve()
    if not project.is_dir():
        raise ValueError(f"KiCad project folder does not exist: {project}")
    directory = _inside(project / _LIBRARY, project)
    directory.mkdir(exist_ok=True)
    table_paths = [_inside(project / name, project) for name in ("sym-lib-table", "fp-lib-table")]
    lock = _inside(directory / ".import.lock", project)
    with _project_lock(lock):
        _recover_import(project, directory)
        return _import_locked(component_id, project, directory, table_paths, print_func, overwrite)


def _import_locked(component_id, project, directory, table_paths, print_func, overwrite):
    work = None
    try:
        originals = {path: _read_optional(path) for path in table_paths}
        prefix = "${KIPRJMOD}/LCSC/LCSC"
        updates = {
            table_paths[0]: _table_update(
                originals[table_paths[0]], "sym_lib_table", _LIBRARY, prefix + ".kicad_sym"
            ),
            table_paths[1]: _table_update(
                originals[table_paths[1]], "fp_lib_table", _LIBRARY, prefix + ".pretty"
            ),
        }
        manifest_path = _inside(directory / _MANIFEST, project)
        symbol_path = _inside(directory / "LCSC.kicad_sym", project)
        originals[manifest_path] = _read_optional(manifest_path)
        originals[symbol_path] = _read_optional(symbol_path)
        manifest = _shared_manifest(directory)
        # Refuse to reconstruct a damaged shared library and lose unrelated symbols.
        symbol_text = (
            originals[symbol_path].decode("utf-8")
            if originals[symbol_path] is not None
            else ('(kicad_symbol_lib (version 20231120) (generator "lcsc_kicad_bridge")\n)\n')
        )
        symbols = _symbol_forms(symbol_text)
        record = manifest["components"].get(component_id)
        if originals[symbol_path] is None and any(
            key != component_id for key in manifest["components"]
        ):
            raise FileNotFoundError(
                "Shared LCSC.kicad_sym is missing; restore it before importing."
            )
        already_present = bool(
            record and _component_complete(directory, record, symbols) and not overwrite
        )
        retry_models = bool(
            already_present and not record["models"] and record.get("model_status") != "unavailable"
        )
        if not already_present or retry_models:
            work = Path(tempfile.mkdtemp(prefix=f".{component_id}-", dir=directory))
            paths = None if overwrite or retry_models else _legacy_copy(project, component_id, work)
            if paths is None:
                paths = _convert_component(component_id, work, _LIBRARY, print_func)
            else:
                print_func(f"{component_id}: reusing the existing project import")
            new_record, new_symbols, files = _prepare_component(component_id, work, paths)
            if retry_models:
                new_record, files = _merge_retried_models(directory, record, new_record, files)
                already_present = not bool(new_record["models"])
            else:
                updates[symbol_path] = _merge_symbols(
                    symbol_text, new_symbols, record["symbols"] if record else {}, symbols
                ).encode("utf-8")
            old_files = record["files"] if record else {}
            for relative in set(files) | (set() if retry_models else set(old_files)):
                path = _inside(directory / relative, project)
                before = _read_optional(path)
                originals[path] = before
                if before is not None and (
                    relative not in old_files or _digest(before) != old_files[relative]
                ):
                    raise FileExistsError(
                        f"Import would overwrite an existing or edited file: {path}"
                    )
                updates[path] = files.get(relative)
            manifest["components"][component_id] = record = new_record
            updates[manifest_path] = json.dumps(manifest, indent=2, ensure_ascii=False).encode(
                "utf-8"
            )
        # Detect edits made while network conversion ran, before changing any file.
        for path, before in originals.items():
            _inside(path, project)
            if _read_optional(path) != before:
                raise RuntimeError(f"Library file changed during import; retry: {path}")
        _commit_import(project, directory, updates, originals)
        assert record is not None
        print_func(
            f"{component_id}: {'already installed' if already_present else 'installed'} in {directory}"
        )
        return ImportResult(
            component_id, _LIBRARY, directory, already_present, bool(record["models"])
        )
    finally:
        if work is not None and work.exists():
            _inside(work, directory)
            shutil.rmtree(work)
