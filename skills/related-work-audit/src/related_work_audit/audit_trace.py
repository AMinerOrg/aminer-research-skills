"""Private, append-only run evidence; never serialize client credentials."""

from __future__ import annotations

import hashlib
import json
import os
from threading import Lock
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__


def implementation_identity() -> dict[str, str]:
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    files = [(f"src/related_work_audit/{path.name}", path) for path in Path(__file__).resolve().parent.glob("*.py")]
    files += [(name, root / name) for name in ("scripts/run.py", "SKILL.md", "pyproject.toml")]
    for name, path in sorted(files):
        if path.is_file():
            digest.update(name.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return {
        "skill_name": "related-work-audit", "skill_version": __version__,
        "implementation": "standalone_related_work_audit",
        "entrypoint": "related_work_audit.cli",
        "source_sha256": digest.hexdigest(),
    }


class AuditTrace:
    def __init__(self, path: Path, secrets: tuple[str, ...] = ()):
        self.path = path
        self.secrets = [value for value in secrets if value]
        self._write_lock = Lock()

    def __call__(self, event: dict[str, Any]) -> None:
        def sanitize(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: "[REDACTED]" if str(key).casefold() in {
                        "authorization", "api_key", "password", "cookie", "set-cookie"
                    } else sanitize(item)
                    for key, item in value.items()
                }
            if isinstance(value, (list, tuple)):
                return [sanitize(item) for item in value]
            if isinstance(value, str):
                for secret in self.secrets:
                    value = value.replace(secret, "[REDACTED]")
            return value

        payload = {"at": datetime.now(timezone.utc).isoformat(), **sanitize(event)}
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        with self._write_lock, os.fdopen(os.open(self.path, flags, 0o600), "a", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
