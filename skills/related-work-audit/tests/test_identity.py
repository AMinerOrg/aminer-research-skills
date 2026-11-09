from __future__ import annotations

import pytest

from related_work_audit import __version__
from related_work_audit.identity import aminer_headers, detect_skill_runtime


RUNTIME_MARKERS = (
    "AMINER_SKILL_RUNTIME", "CLAUDE_CODE", "CLAUDECODE", "CLAUDE_PROJECT_DIR",
    "CURSOR_TRACE_ID", "CURSOR_AGENT", "CODEX_HOME", "CODEX_CLI",
    "OPENCLAW", "OPENCLAW_HOME",
)


@pytest.fixture(autouse=True)
def clear_runtime_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in RUNTIME_MARKERS:
        monkeypatch.delenv(name, raising=False)


def test_aminer_headers_include_skill_identity_and_unknown_fallback() -> None:
    headers = aminer_headers("test-key")
    assert headers["Authorization"] == "test-key"
    assert headers["X-Platform"] == "unknown"
    assert headers["X-Skill-Name"] == "related-work-audit"
    assert headers["X-Skill-Version"] == __version__
    assert headers["Content-Type"] == "application/json;charset=utf-8"


def test_explicit_override_precedes_host_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMINER_SKILL_RUNTIME", " OpenClaw ")
    monkeypatch.setenv("CLAUDE_CODE", "1")
    assert detect_skill_runtime() == "openclaw"


@pytest.mark.parametrize("marker,expected", [
    ("CLAUDE_CODE", "claude-code"),
    ("CURSOR_AGENT", "cursor"),
    ("CODEX_HOME", "codex"),
    ("OPENCLAW", "openclaw"),
])
def test_detects_supported_hosts(
    monkeypatch: pytest.MonkeyPatch, marker: str, expected: str
) -> None:
    monkeypatch.setenv(marker, "1")
    assert detect_skill_runtime() == expected
