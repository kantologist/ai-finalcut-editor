"""Local secrets stored in the app home `.env` (never in settings.json)."""

from __future__ import annotations

import os
import re
from pathlib import Path

from .paths import ROOT

ENV_PATH = ROOT / ".env"
_KEY_LINE = re.compile(r"^\s*OPENAI_API_KEY\s*=\s*(.*)\s*$")


def env_path() -> Path:
    return ENV_PATH


def _parse_env_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value.strip()


def read_openai_api_key() -> str:
    """Read the OpenAI key from `.env`, falling back to the process environment."""
    if ENV_PATH.is_file():
        try:
            for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
                match = _KEY_LINE.match(line)
                if match:
                    return _parse_env_value(match.group(1))
        except OSError:
            pass
    return os.getenv("OPENAI_API_KEY", "").strip()


def write_openai_api_key(api_key: str) -> str:
    """Persist OPENAI_API_KEY to `.env` and update the running process."""
    key = api_key.strip()
    if key and not key.startswith("sk-"):
        raise ValueError("OpenAI API keys usually start with sk-")

    lines: list[str] = []
    replaced = False
    if ENV_PATH.is_file():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if _KEY_LINE.match(line):
                if key:
                    lines.append(f"OPENAI_API_KEY={key}")
                replaced = True
            else:
                lines.append(line)
    if key and not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"OPENAI_API_KEY={key}")

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines).rstrip() + ("\n" if lines else "")
    ENV_PATH.write_text(text, encoding="utf-8")

    if key:
        os.environ["OPENAI_API_KEY"] = key
    else:
        os.environ.pop("OPENAI_API_KEY", None)
    return key


def mask_openai_api_key(api_key: str) -> str:
    key = api_key.strip()
    if not key:
        return ""
    if len(key) <= 12:
        return "•" * len(key)
    return f"{key[:7]}…{key[-4:]}"
