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
    for line in result.stdout.splitlines()[1:]:
        path = line.strip().split(" (", 1)[0].strip()
        if path:
            libs.append(path)
    return libs


def _otool_id(binary: Path) -> str | None:
    result = subprocess.run(
        ["otool", "-D", str(binary)],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    if len(lines) >= 2:
        return lines[1]
    return None


def _is_system(path: str) -> bool:
    return path.startswith(SYSTEM_PREFIXES) or path.startswith("@")


def _resolve(dep: str, loader: Path) -> Path | None:
    if dep.startswith("@loader_path/"):
        return (loader.parent / dep.split("/", 1)[1]).resolve()
    if dep.startswith("@executable_path/"):
        return (loader.parent / dep.split("/", 1)[1]).resolve()
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
        dest_name = current.name
        mapping[current] = DEST / dest_name
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
        if _is_system(dep) or dep.startswith("@loader_path/"):
            continue
        resolved = _resolve(dep, dest)
        if resolved is None:
            # Already copied by basename? try last path component.
            name = Path(dep).name
            if (DEST / name).is_file():
                subprocess.run(
                    ["install_name_tool", "-change", dep, f"@loader_path/{name}", str(dest)],
                    check=False,
                )
            continue
        source = resolved
        # mapping keys are original brew paths
        dest_name = None
        for src, dst in mapping.items():
            if src == source or src.name == source.name:
                dest_name = dst.name
                break
        if dest_name:
            subprocess.run(
                ["install_name_tool", "-change", dep, f"@loader_path/{dest_name}", str(dest)],
                check=False,
            )


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
    bundled_ffprobe = DEST / "ffprobe"
    probe = subprocess.run(
        [str(bundled_ffmpeg), "-version"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        print(probe.stdout, probe.stderr, file=sys.stderr)
        raise SystemExit("Bundled ffmpeg failed to start")
    print(f"Bundled {len(list(DEST.glob('*')))} ffmpeg files into {DEST.relative_to(ROOT)}")
    print(probe.stdout.splitlines()[0])


if __name__ == "__main__":
    main()
