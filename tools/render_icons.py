#!/usr/bin/env python3
"""Render toolbar and PCM PNGs from resources/bridge.svg.

Requires CairoSVG in the development Python environment. Run this after editing
the SVG, then commit the generated PNGs; normal package builds need only Python's
standard library and do not render images or install development dependencies.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = {
    "plugins/icon_24.png": 24,
    "plugins/icon_48.png": 48,
    "resources/icon.png": 64,
}


def main() -> int:
    try:
        import cairosvg
    except ImportError:
        print(
            "Install the development renderer with: python -m pip install cairosvg", file=sys.stderr
        )
        return 1
    source = ROOT / "resources/bridge.svg"
    for relative, size in OUTPUTS.items():
        target = ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        cairosvg.svg2png(
            url=str(source), write_to=str(target), output_width=size, output_height=size
        )
        print(f"Rendered {relative} ({size} x {size})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
