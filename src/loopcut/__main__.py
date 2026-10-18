import sys

if sys.version_info < (3, 10):  # The vendored add-on modules use 3.10 syntax.
    raise SystemExit(f"loopcut needs Python 3.10 or newer (this is {sys.version.split()[0]}). "
                     "Run it with uv instead: uvx loopcut")
from .cli import main

raise SystemExit(main())
