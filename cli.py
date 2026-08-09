#!/usr/bin/env python
"""Thin wrapper so `python cli.py ...` keeps working from a clone.

The real implementation lives in `hotwheels/cli.py` so that installing the
package exposes a `hotwheels` command.
"""

from hotwheels.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
