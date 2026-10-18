"""Loopcut cloud render for agents and scripts: pack a saved .blend with any Blender, render it
on Loopcut's cloud GPUs from the user's Loopcut credits, and download the frames."""

import sys

__version__ = "0.1.0"

if sys.version_info < (3, 10):  # The vendored add-on modules use 3.10 syntax.
    raise SystemExit(f"loopcut-render needs Python 3.10 or newer (this is {sys.version.split()[0]}). "
                     "Run it with uv instead: uvx loopcut-render")
