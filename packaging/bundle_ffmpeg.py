#!/usr/bin/env python3
"""Copy ffmpeg + ffprobe and their non-system dylibs into packaging/ffmpeg/."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "packaging" / "ffmpeg"
SYSTEM_PREFIXES = ("/usr/lib", "/System/", "/Library/Apple/")


def _which(name: str) -> Path:
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"{name} not found on PATH. Install with: brew install ffmpeg")
    return Path(found).resolve()


def _otool_libs(binary: Path) -> list[str]:
    result = subprocess.run(
        ["otool", "-L", str(binary)],
        check=True,
        capture_output=True,
        text=True,
    )
    libs: list[str] = []
    lines = result.stdout.splitlines()
    for line in lines[1:]:
        path = line.strip().split(" (", 1)[0].strip()
        if path:
            libs.append(path)
    return libs


def _otool_rpaths(binary: Path) -> list[Path]:
    result = subprocess.run(
        ["otool", "-l", str(binary)],
        check=True,
        capture_output=True,
        text=True,
    )
    rpaths: list[Path] = []
    lines = result.stdout.splitlines()
    for index, line in enumerate(lines):
        if "cmd LC_RPATH" not in line:
            continue
        for follow in lines[index + 1 : index + 6]:
            text = follow.strip()
            if text.startswith("path "):
                raw = text.split("path ", 1)[1].split(" (", 1)[0].strip()
                if raw.startswith("@loader_path"):
                    raw = raw.replace("@loader_path", str(binary.parent), 1)
                elif raw.startswith("@executable_path"):
                    raw = raw.replace("@executable_path", str(binary.parent), 1)
                path = Path(raw)
                if path.is_dir():
                    rpaths.append(path)
                break
    return rpaths


def _is_bundled_relative(path: str) -> bool:
    return path.startswith(("@loader_path/", "@executable_path/", "@rpath/"))


def _is_system(path: str) -> bool:
    if _is_bundled_relative(path):
        return False
    return path.startswith(SYSTEM_PREFIXES) or path.startswith("@")


def _resolve(dep: str, loader: Path) -> Path | None:
    if dep.startswith("@loader_path/"):
        rest = dep.split("/", 1)[1] if "/" in dep else ""
        candidate = (loader.parent / rest).resolve()
        return candidate if candidate.is_file() else None
    if dep.startswith("@executable_path/"):
        rest = dep.split("/", 1)[1] if "/" in dep else ""
        candidate = (loader.parent / rest).resolve()
        return candidate if candidate.is_file() else None
    if dep.startswith("@rpath/"):
        name = dep.split("/", 1)[1] if "/" in dep else Path(dep).name
        for root in _otool_rpaths(loader):
            candidate = (root / name).resolve()
            if candidate.is_file():
                return candidate
        return None
    raw = Path(dep)
    if raw.is_file():
        return raw.resolve()
    return None


def _collect(seed: Path) -> dict[Path, Path]:
    """Map source files -> dest filenames (basename)."""
    mapping: dict[Path, Path] = {}
    queue = [seed]
    seen: set[Path] = set()
    while queue:
        current = queue.pop()
        current = current.resolve()
        if current in seen or not current.is_file():
            continue
        seen.add(current)
        mapping[current] = DEST / current.name
        for dep in _otool_libs(current):
            if _is_system(dep):
                continue
            resolved = _resolve(dep, current)
            if resolved is None:
                print(f"warning: unresolved dependency {dep} from {current}", file=sys.stderr)
                continue
            if resolved not in seen:
                queue.append(resolved)
    return mapping


def _rewrite(dest: Path, mapping: dict[Path, Path]) -> None:
    subprocess.run(["chmod", "u+w", str(dest)], check=False)
    new_id = f"@loader_path/{dest.name}"
    subprocess.run(["install_name_tool", "-id", new_id, str(dest)], check=False)
    for dep in _otool_libs(dest):
        if dep.startswith("@loader_path/") or _is_system(dep):
            continue
        resolved = _resolve(dep, dest)
        dest_name = None
        if resolved is not None:
            for src, dst in mapping.items():
                if src == resolved or src.name == resolved.name:
                    dest_name = dst.name
                    break
        if dest_name is None:
            name = Path(dep).name
            if (DEST / name).is_file():
                dest_name = name
        if dest_name:
            subprocess.run(
                ["install_name_tool", "-change", dep, f"@loader_path/{dest_name}", str(dest)],
                check=False,
            )


def _first_line(*chunks: str) -> str:
    for chunk in chunks:
        for line in (chunk or "").splitlines():
            if line.strip():
                return line.strip()
    return ""


def main() -> None:
    ffmpeg = _which("ffmpeg")
    ffprobe = _which("ffprobe")
    DEST.mkdir(parents=True, exist_ok=True)
    for leftover in DEST.iterdir():
        if leftover.name == "README.md":
            continue
        if leftover.is_file():
            leftover.unlink()

    mapping: dict[Path, Path] = {}
    mapping.update(_collect(ffmpeg))
    mapping.update(_collect(ffprobe))

    for src, dst in mapping.items():
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
        subprocess.run(["codesign", "--remove-signature", str(dst)], check=False, capture_output=True)

    for dst in mapping.values():
        _rewrite(dst, mapping)
        subprocess.run(
            ["codesign", "--force", "--sign", "-", str(dst)],
            check=False,
            capture_output=True,
        )

    bundled_ffmpeg = DEST / "ffmpeg"
    if not bundled_ffmpeg.is_file():
        raise SystemExit("ffmpeg was not copied into packaging/ffmpeg")

    probe = subprocess.run(
        [str(bundled_ffmpeg), "-version"],
        capture_output=True,
        text=True,
    )
    version = _first_line(probe.stdout, probe.stderr)
    if probe.returncode != 0:
        print(probe.stdout, probe.stderr, file=sys.stderr)
        raise SystemExit("Bundled ffmpeg failed to start")
    print(f"Bundled {len(list(DEST.glob('*')))} ffmpeg files into {DEST.relative_to(ROOT)}")
    print(version or "ffmpeg bundled")


if __name__ == "__main__":
    main()
