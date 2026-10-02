#!/usr/bin/env bash
# Compatibility entrypoint: both build commands use the same pinned sources.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" == "--no-update" ]]; then
    shift
fi

if command -v python3 >/dev/null 2>&1; then
    exec python3 "$script_dir/tools/build_lcsc_plugin.py" "$@"
fi
exec python "$script_dir/tools/build_lcsc_plugin.py" "$@"