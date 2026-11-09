#!/usr/bin/env python3
"""Run this Skill directly without a repository-level installation."""

import sys
from pathlib import Path

skill_src = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(skill_src))

from related_work_audit.cli import main  # noqa: E402

raise SystemExit(main())
