"""Small, side-effect-free configuration layer for the standalone Skill."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelProfile:
    name: str
    label: str
    model: str
    api_key: str
    base_url: str
    thinking_effort: str | None = None


def _first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE assignments without shell evaluation."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Environment file not found: {path}")
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"Invalid environment assignment at {path}:{number}")
        name, value = line.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"Invalid environment name at {path}:{number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(name, value)


def resolve_model_profile(name: str) -> ModelProfile:
    selected = name.strip().lower()
    if selected == "deepseek":
        profile = ModelProfile(
            name="deepseek",
            label="DeepSeek Chat",
            model=_first("DEEPSEEK_MODEL", default="deepseek-chat"),
            api_key=_first("DEEPSEEK_API_KEY"),
            base_url=_first("DEEPSEEK_BASE_URL", default="https://api.deepseek.com"),
        )
    elif selected == "glm":
        profile = ModelProfile(
            name="glm",
            label="GLM 5.2",
            model=_first("GLM_MODEL", default="glm-5.2"),
            api_key=_first("GLM_API_KEY"),
            base_url=_first("GLM_BASE_URL", default="https://open.bigmodel.cn/api/paas/v4"),
        )
    elif selected in {"glm53", "glm53flash"}:
        effort = _first("GLM53_THINKING_EFFORT", default="low").lower()
        if effort not in {"low", "high", "max"}:
            raise ValueError("GLM53_THINKING_EFFORT must be low, high, or max.")
        flash = selected == "glm53flash"
        profile = ModelProfile(
            name=selected,
            label=f"GLM 5.3{' Flash' if flash else ''} ({effort} reasoning)",
            model=_first("GLM53_FLASH_MODEL" if flash else "GLM53_MODEL", default="glm-5.3-flash" if flash else "glm-5.3"),
            api_key=_first("GLM_API_KEY"),
            base_url=_first("GLM_BASE_URL", default="https://open.bigmodel.cn/api/paas/v4"),
            thinking_effort=effort,
        )
    elif selected == "gpt":
        profile = ModelProfile(
            name="gpt",
            label="GPT-5.6 Sol",
            model=_first("GPT_MODEL", default="gpt-5.6-sol"),
            api_key=_first("GPT_API_KEY"),
            base_url=_first("GPT_BASE_URL"),
        )
    else:
        raise ValueError(f"Unknown model profile: {name}")
    if not profile.api_key or not profile.base_url:
        raise ValueError(f"Model profile {profile.name} is not configured.")
    return profile


def proxy_url() -> str | None:
    return _first("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy") or None


def grobid_url() -> str:
    return _first("GROBID_URL", default="http://127.0.0.1:8070").rstrip("/")


def aminer_api_key() -> str:
    """Return only the dedicated AMiner credential."""
    value = _first("AMINER_API_KEY")
    if not value:
        raise ValueError(
            "AMINER_API_KEY is not configured; --pub-id requires an AMiner API key."
        )
    return value


def default_work_root() -> Path:
    configured = _first("RELATED_WORK_AUDIT_WORK_DIR")
    root = Path(configured).expanduser() if configured else Path.home() / ".local" / "share" / "related-work-audit"
    return root.resolve()


def openai_timeout_seconds() -> int:
    try:
        return max(30, int(_first("OPENAI_TIMEOUT_SECONDS", default="300")))
    except ValueError:
        return 300


def openai_max_retries() -> int:
    try:
        return max(0, int(_first("OPENAI_MAX_RETRIES", default="0")))
    except ValueError:
        return 0
