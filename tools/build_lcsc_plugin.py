#!/usr/bin/env python3
"""Build the LCSC KiCad Bridge IPC installation ZIP without network access.

Run from any directory with Python 3.10 or newer.  Only the generated ZIP is
changed; source metadata and pinned submodules are never updated by this tool.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.4.0"
MIN_KICAD = "10.0.0"
IDENTIFIER = "com.github.lcsc-kicad.browser"
TOOLBAR_ICONS = {"icon_24.png": 24, "icon_48.png": 48}
# KiCad 10 inserts IPC icon bundles directly, bypassing its normal toolbar icon
# size normalization. Use a single native-sized icon to avoid cropped IPC buttons;
# retain the 48 pixel asset for a future host that handles multiple sizes reliably.
ACTION_ICONS = ("icon_24.png",)
ZIP_TIMESTAMP = (2026, 9, 29, 0, 0, 0)
SUBMODULE_PACKAGES = (Path("plugins/easyeda2kicad/easyeda2kicad"),)
SUBMODULE_ROOTS = {"easyeda2kicad"}
EXCLUDED_DIRS = {
    "__pycache__",
    "cache",
    "logs",
    "test",
    "tests",
    "doc",
    "docs",
    "examples",
    "build",
    "dist",
    "venv",
    "node_modules",
}
RUNTIME_SUFFIXES = {".py", ".json", ".txt", ".png", ".ico", ".svg"}
# Keep the local application shell complete, while excluding preview pages and
# developer files. Explicit entries also preserve Bootstrap's nonstandard suffix.
UI_FILES = (
    "plugins/ui/index.html",
    "plugins/ui/shell.css",
    "plugins/ui/shell.js",
    "plugins/ui/vendor/bootstrap.min.css",
    "plugins/ui/vendor/LICENSE.bootstrap",
)


class BuildError(Exception):
    """A source checkout is incomplete or unsuitable for packaging."""


def check_sources(root: Path) -> None:
    missing = [
        str(package / "__init__.py")
        for package in SUBMODULE_PACKAGES
        if not (root / package / "__init__.py").is_file()
    ]
    if missing:
        raise BuildError(
            "Missing pinned submodule sources: "
            + ", ".join(missing)
            + ". Run 'git submodule update --init --recursive' in the checkout "
            "before building. This build does not initialize or update submodules."
        )
    required = (
        "metadata.json",
        "resources/icon.png",
        "plugins/plugin.json",
        "plugins/lcsc_launch.py",
        "plugins/lcsc_browser.py",
        "plugins/lcsc_ui.py",
        "plugins/lcsc_page.py",
        "plugins/lcsc_web_bridge.py",
        "plugins/lcsc_project_import.py",
        "plugins/lcsc_download_cache.py",
        "plugins/lcsc_model_download.py",
        "plugins/impart_easyeda.py",
        "plugins/requirements.txt",
        "LICENSE.txt",
        "THIRD_PARTY_NOTICES.md",
        "README.md",
        "plugins/easyeda2kicad/LICENSE",
        "doc/LCSC_BROWSER_zh_CN.md",
        *UI_FILES,
    )
    for relative in required:
        if not (root / relative).is_file():
            raise BuildError(f"Required package file is missing: {relative}")
    for relative in UI_FILES:
        path = root / relative
        if (
            not path.stat().st_size
            or path.is_symlink()
            or not path.resolve().is_relative_to(root.resolve())
        ):
            raise BuildError(f"UI resource must be a nonempty local file: {relative}")
    for relative, size in {
        "resources/icon.png": 64,
        **{f"plugins/{name}": size for name, size in TOOLBAR_ICONS.items()},
    }.items():
        path = root / relative
        if not path.is_file():
            raise BuildError(f"Required icon is missing: {relative}; run tools/render_icons.py.")
        data = path.read_bytes()
        if (
            len(data) < 24
            or data[:8] != b"\x89PNG\r\n\x1a\n"
            or data[12:16] != b"IHDR"
            or struct.unpack(">II", data[16:24]) != (size, size)
        ):
            raise BuildError(
                f"{relative} must be a {size} x {size} PNG; run tools/render_icons.py."
            )
    manifest = json.loads((root / "plugins/plugin.json").read_text(encoding="utf-8"))
    if manifest.get("identifier") != IDENTIFIER:
        raise BuildError("plugins/plugin.json has an unexpected plugin identifier.")
    actions = manifest.get("actions", [])
    if len(actions) != 1 or actions[0].get("entrypoint") != "lcsc_launch.py":
        raise BuildError("plugins/plugin.json must contain only the lcsc_launch.py action.")
    if set(actions[0].get("scopes", [])) != {"schematic", "pcb"}:
        raise BuildError("The IPC action must be available in schematic and PCB editors.")
    for theme in ("icons-light", "icons-dark"):
        if actions[0].get(theme) != list(ACTION_ICONS):
            raise BuildError(f"The IPC action {theme} must use only the 24 pixel toolbar icon.")
    # PCM defaults to the legacy PCB-only runtime when this flag is missing.
    # Reject that regression before emitting an otherwise installable ZIP.
    metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    if (
        metadata.get("$schema") != "https://go.kicad.org/pcm/schemas/v2"
        or metadata.get("identifier") != IDENTIFIER
        or metadata.get("type") != "plugin"
    ):
        raise BuildError("metadata.json must describe the standalone Bridge plugin.")
    versions = metadata.get("versions", [])
    if len(versions) != 1:
        raise BuildError("metadata.json must contain one installable version.")
    version = versions[0]
    if (
        version.get("version") != VERSION
        or version.get("kicad_version") != MIN_KICAD
        or version.get("runtime") != "ipc"
    ):
        raise BuildError(
            f"metadata.json must declare version {VERSION}, KiCad {MIN_KICAD} and runtime ipc."
        )


def runtime_files(directory: Path):
    """Select runtime files, never shipping local settings or development data."""
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if any(part.startswith(".") or part.lower() in EXCLUDED_DIRS for part in relative.parts):
            continue
        # A linked file/directory must not pull unrelated local data into the ZIP.
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            continue
        if not path.is_file() or path.suffix.lower() not in RUNTIME_SUFFIXES:
            continue
        if path.suffix.lower() in {".json", ".txt"} and path.name.lower().startswith(
            ("config.", "settings.", "local_settings.")
        ):
            continue
        yield path


def package_files(root: Path) -> dict[str, Path]:
    """Allow only Bridge modules and the converter; exclude retired plugin code."""
    files: dict[str, Path] = {}
    for name in ("icon.png", "icon.svg"):
        path = root / "resources" / name
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path
    plugin_root = root / "plugins"
    allowed_names = {
        "__init__.py",
        "__main__.py",
        "plugin.json",
        "requirements.txt",
        "impart_easyeda.py",
        *TOOLBAR_ICONS,
    }
    for path in sorted(plugin_root.iterdir()):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name in allowed_names or (path.name.startswith("lcsc_") and path.suffix == ".py"):
            files[path.relative_to(root).as_posix()] = path
    for relative in SUBMODULE_PACKAGES:
        for path in runtime_files(root / relative):
            files[path.relative_to(root).as_posix()] = path
    for relative in UI_FILES:
        files[relative] = root / relative
    # Keep all license texts with the installed plugin, including GPL/AGPL
    # notices for the bundled source packages.
    files["plugins/LICENSE.txt"] = root / "LICENSE.txt"
    files["plugins/THIRD_PARTY_NOTICES.md"] = root / "THIRD_PARTY_NOTICES.md"
    files["plugins/README.md"] = root / "README.md"
    for submodule in sorted(SUBMODULE_ROOTS):
        relative = f"plugins/{submodule}/LICENSE"
        files[relative] = root / relative
    files["plugins/doc/LCSC_BROWSER_zh_CN.md"] = root / "doc/LCSC_BROWSER_zh_CN.md"
    return files


def package_metadata(root: Path) -> bytes:
    """Use the checked-in identity and runtime metadata without rewriting it."""
    metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    return (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data)


def build(output: Path, root: Path = ROOT) -> int:
    check_sources(root)
    files = package_files(root)
    metadata = package_metadata(root)
    output = output.resolve()
    # Avoid accidentally replacing a source file via --output.
    if output.suffix.lower() != ".zip":
        raise BuildError("Output must have a .zip extension.")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        # Closing first also permits opening/replacing the file on Windows.
        with tempfile.NamedTemporaryFile(
            prefix=".lcsc-build-", suffix=".zip", dir=output.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
        with zipfile.ZipFile(temporary, "w") as archive:
            write_entry(archive, "metadata.json", metadata)
            for name, path in sorted(files.items()):
                write_entry(archive, name, path.read_bytes())
        temporary.replace(output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return len(files) + 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist/LCSC-KiCad-Bridge.zip",
        help="Output ZIP path (default: checkout/dist/LCSC-KiCad-Bridge.zip)",
    )
    args = parser.parse_args()
    try:
        count = build(args.output)
    except (BuildError, OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"Build failed: {error}", file=sys.stderr)
        return 1
    print(f"Built {args.output.resolve()} ({count} files, version {VERSION})")
    print("Source metadata and submodules were not modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
