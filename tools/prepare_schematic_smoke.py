#!/usr/bin/env python3
"""Prepare (never launch) an isolated Windows KiCad schematic IPC smoke test.

All writable editor settings, documents, cache, socket files and the copied
plugin environment live under build/. The installed KiCad Python environment
is only read and copied. Run the resulting launch-spec.json with an explicit
subprocess environment to open the test editor; this helper opens no windows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER = "com.github.lcsc-kicad.browser"


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare(base: Path, package: Path, kicad: Path, source_venv: Path) -> dict:
    base = base.resolve()
    build = (ROOT / "build").resolve()
    if not base.is_relative_to(build) or base == build:
        raise ValueError("The test directory must be a child of this checkout's build directory")
    if base.exists():
        raise ValueError("The test directory already exists; choose a new --output directory")
    for required in (
        package,
        kicad / "eeschema.exe",
        kicad / "pythonw.exe",
        source_venv / "pyvenv.cfg",
    ):
        if not required.is_file():
            raise FileNotFoundError(required)

    config = base / "config/10.0"
    documents = base / "documents/KiCad/10.0"
    cache = base / "cache/KiCad/10.0"
    temporary = base / "temp"
    third_party = documents / "3rdparty"
    plugin = third_party / "plugins" / IDENTIFIER.replace(".", "_")
    venv = cache / "python-environments" / IDENTIFIER
    project = base / "project"
    for directory in (config, plugin, temporary, project):
        directory.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(package) as archive:
        metadata = json.loads(archive.read("metadata.json"))
        if metadata.get("identifier") != IDENTIFIER:
            raise ValueError("The package must be the new LCSC KiCad Bridge package")
        for member in archive.infolist():
            relative = PurePosixPath(member.filename)
            if not relative.parts or relative.parts[0] != "plugins" or member.is_dir():
                continue
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe archive member")
            destination = plugin.joinpath(*relative.parts[1:])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(member))

    # A copy is intentional: KiCad may recreate or install into this environment.
    # A junction to the live user environment would not provide isolation.
    shutil.copytree(source_venv, venv, ignore=shutil.ignore_patterns("__pycache__"))
    (venv / "pip.ini").write_text(
        "[global]\nno-index = true\ndisable-pip-version-check = true\n", encoding="utf-8"
    )
    write_json(
        config / "kicad_common.json",
        {
            "meta": {"version": 6},
            "api": {"enable_server": True, "interpreter_path": str(kicad / "pythonw.exe")},
            "environment": {"vars": {"KICAD10_3RD_PARTY": str(third_party)}},
            "system": {"language": "简体中文", "working_dir": str(project)},
        },
    )
    # Empty tables avoid the first-run global library configuration dialog.
    (config / "sym-lib-table").write_text("(sym_lib_table (version 7))\n", encoding="utf-8")
    (config / "fp-lib-table").write_text("(fp_lib_table (version 7))\n", encoding="utf-8")
    write_json(project / "schematic-ipc-test.kicad_pro", {"meta": {"version": 1}})
    schematic = project / "schematic-ipc-test.kicad_sch"
    schematic.write_text(
        '(kicad_sch (version 20250114) (generator "eeschema")\n'
        f'  (uuid "{uuid.uuid4()}")\n'
        '  (paper "A4")\n'
        "  (lib_symbols)\n"
        '  (sheet_instances (path "/" (page "1")))\n'
        ")\n",
        encoding="utf-8",
    )
    environment = {
        "KICAD_CONFIG_HOME": str(base / "config"),
        "KICAD_DOCUMENTS_HOME": str(base / "documents"),
        "KICAD_CACHE_HOME": str(base / "cache"),
        "KICAD10_3RD_PARTY": str(third_party),
        "TEMP": str(temporary),
        "TMP": str(temporary),
    }
    spec = {
        "executable": str(kicad / "eeschema.exe"),
        "arguments": [str(schematic)],
        "working_directory": str(project),
        "environment": environment,
        "remove_environment": ["KICAD_API_SOCKET", "KICAD_API_TOKEN", "PYTHONHOME", "PYTHONPATH"],
        "plugin": str(plugin),
        "venv": str(venv),
        "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
    }
    write_json(base / "launch-spec.json", spec)
    (base / "launch.py").write_text(
        '"""Explicitly launch the prepared, isolated schematic smoke-test editor."""\n'
        "import json, os, subprocess\n"
        "from pathlib import Path\n"
        "base = Path(__file__).resolve().parent\n"
        'spec = json.loads((base / "launch-spec.json").read_text(encoding="utf-8"))\n'
        "environment = dict(os.environ)\n"
        'for name in spec["remove_environment"]:\n'
        "    environment.pop(name, None)\n"
        'environment.update(spec["environment"])\n'
        'with (base / "editor-output.log").open("ab") as log:\n'
        '    process = subprocess.Popen([spec["executable"], *spec["arguments"]],\n'
        '        cwd=spec["working_directory"], env=environment, stdout=log, stderr=log)\n'
        '(base / "editor.pid").write_text(str(process.pid), encoding="ascii")\n'
        'print(f"Isolated eeschema PID: {process.pid}", flush=True)\n'
        "raise SystemExit(process.wait())\n",
        encoding="utf-8",
    )

    child_environment = dict(os.environ)
    for key in spec["remove_environment"]:
        child_environment.pop(key, None)
    child_environment.update(environment)
    child_environment["VIRTUAL_ENV"] = str(venv)
    python = venv / "Scripts/python.exe"
    checks = (
        [str(python), "-B", str(plugin / "lcsc_launch.py"), "--check"],
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-input",
            "--isolated",
            "--no-index",
            "--disable-pip-version-check",
            "--only-binary",
            ":all:",
            "--require-virtualenv",
            "-r",
            str(plugin / "requirements.txt"),
        ],
    )
    logs = []
    for command in checks:
        result = subprocess.run(
            command, cwd=project, env=child_environment, text=True, capture_output=True, check=False
        )
        logs.append(
            {
                "command": command,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        if result.returncode:
            write_json(base / "preflight.json", logs)
            raise RuntimeError("The isolated plugin runtime failed preflight; see preflight.json")
    write_json(base / "preflight.json", logs)
    return spec


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build/schematic-test")
    parser.add_argument("--package", type=Path, default=ROOT / "dist/LCSC-KiCad-Bridge.zip")
    parser.add_argument("--kicad-bin", type=Path, default=Path("D:/KiCad/bin"))
    parser.add_argument(
        "--source-venv",
        type=Path,
        default=Path(os.environ["LOCALAPPDATA"])
        / "KiCad/10.0/python-environments/com.github.Steffen-W.impartGUI",
    )
    args = parser.parse_args()
    prepare(args.output, args.package, args.kicad_bin, args.source_venv)
    print(f"Prepared {args.output.resolve() / 'launch-spec.json'}; no editor was launched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
