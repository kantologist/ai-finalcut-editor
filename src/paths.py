"""Resolve project / user-data roots for CLI, web UI, and packaged desktop builds."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "AI Final Cut Editor"
APP_SUPPORT_NAME = "AIFinalCutEditor"

WORKSPACE_DIRS = (
    "workspace/originals",
    "workspace/proxies",
    "workspace/proxies/thumbs",
    "workspace/proxies/stills",
    "workspace/frames",
    "workspace/metadata",
    "workspace/analysis",
    "workspace/edits",
    "workspace/output",
    "prompts",
)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Read-only resources shipped with the app (PyInstaller) or the repo root."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parents[1]


def resolve_root() -> Path:
    """Writable project home: repo checkout, AI_EDIT_HOME, or Application Support."""
    override = os.environ.get("AI_EDIT_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if is_frozen():
        if sys.platform == "darwin":
            return (Path.home() / "Library" / "Application Support" / APP_SUPPORT_NAME).resolve()
        return (Path.home() / f".{APP_SUPPORT_NAME.lower()}").resolve()
    return Path(__file__).resolve().parents[1]


def ensure_app_home(root: Path | None = None) -> Path:
    """Create workspace folders and seed default prompts/settings on first launch."""
    root = (root or resolve_root()).resolve()
    for rel in WORKSPACE_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)

    src_prompts = bundle_root() / "prompts"
    dst_prompts = root / "prompts"
    if src_prompts.is_dir():
        for path in src_prompts.glob("*.md"):
            target = dst_prompts / path.name
            if not target.is_file():
                shutil.copy2(path, target)

    settings = root / "workspace" / "settings.json"
    for candidate in (
        bundle_root() / "workspace" / "settings.json",
        bundle_root() / "packaging" / "default_settings.json",
    ):
        if not settings.is_file() and candidate.is_file():
            shutil.copy2(candidate, settings)
            break

    return root


ROOT = resolve_root()
