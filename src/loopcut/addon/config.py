"""Stand-in for the add-on's config module: only the service URL that cloud_http reads."""

import os

SERVICE_URL = os.environ.get("LOOPCUT_SERVICE_URL", "https://loopcut.org").rstrip("/")
