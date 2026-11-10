"""Make the src-layout package importable during repository-root collection."""

from __future__ import annotations

import sys
from pathlib import Path


src = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(src))
