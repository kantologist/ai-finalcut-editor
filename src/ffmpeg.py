"""Locate bundled ffmpeg/ffprobe, then fall back to PATH (developer installs)."""

from __future__ import annotations

import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path

from .paths import bundle_root, is_frozen


def _candidates(name: str) -> list[Path]:
    roots: list[Path] = [bundle_root()]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    if is_frozen():
        roots.append(Path(sys.executable).resolve().parent)
        # PyInstaller 6 app bundle: Contents/MacOS and Contents/Frameworks.
        exe_parent = Path(sys.executable).resolve().parent
        roots.append(exe_parent / "_internal")
        if exe_parent.name == "MacOS":
            roots.append(exe_parent.parent / "Frameworks")
            roots.append(exe_parent.parent / "Resources")

    paths: list[Path] = []
    for root in roots:
        paths.extend(
            [
                root / name,
                root / "ffmpeg" / name,
                root / "bin" / name,
            ]
        )
    return paths


@lru_cache(maxsize=4)
def ffmpeg_binary(name: str) -> str:
    """Return an absolute path to ffmpeg or ffprobe."""
    override = os.environ.get(f"AI_EDIT_{name.upper()}", "").strip()
    if override:
        path = Path(override).expanduser()
        if path.is_file():
            return str(path.resolve())

    for candidate in _candidates(name):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())

    found = shutil.which(name)
    if found:
        return found

    raise FileNotFoundError(
        f"{name} is not available. The Mac app should bundle it; from source, install ffmpeg "
        "(e.g. brew install ffmpeg)."
    )


def ffmpeg_exe() -> str:
    return ffmpeg_binary("ffmpeg")


def ffprobe_exe() -> str:
    return ffmpeg_binary("ffprobe")
