"""Allow launching the browser with ``python -m plugins`` in a source checkout."""

from .lcsc_launch import main

if __name__ == "__main__":
    raise SystemExit(main())
