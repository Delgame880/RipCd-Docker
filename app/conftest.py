"""Pytest bootstrap: make the ``ripcd`` package importable without installing it."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# Keep the tests hermetic: never touch the real /config or a real CD drive.
os.environ.setdefault("RIPCD_CONFIG_DIR", "/tmp/ripcd-tests-config")
