"""Generate cheap AI frame proxies from videos in the media inventory.

Samples roughly one frame every 2 seconds (scaled to width 768) under
workspace/frames/. This avoids shipping original 4K media to a vision model.

Future: replace uniform sampling with scene-change detection and pick
representative frames around each detected shot.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel

from .ffmpeg import ffmpeg_exe
from .paths import ROOT

METADATA_PATH = ROOT / "workspace" / "metadata" / "media.json"
FRAMES_DIR = ROOT / "workspace" / "frames"

# Uniform temporal sample: 1 frame / 2 seconds.
SAMPLE_FPS = "1/2"
SCALE_WIDTH = 768


class MediaRecord(BaseModel):
    id: str
    path: str
    type: str
    duration: float | None = None


def load_videos(metadata_path: Path = METADATA_PATH) -> list[MediaRecord]:
    if not metadata_path.is_file():
        raise FileNotFoundError(
            f"Missing media inventory: {metadata_path}. Run inspect_media.py first."
        )
    records = [MediaRecord.model_validate(row) for row in json.loads(metadata_path.read_text())]
    return [r for r in records if r.type == "video"]


def frame_pattern(media_id: str, frames_dir: Path = FRAMES_DIR) -> Path:
    return frames_dir / f"{media_id}_%04d.jpg"


def existing_frames(media_id: str, frames_dir: Path = FRAMES_DIR) -> list[Path]:
    return sorted(frames_dir.glob(f"{media_id}_*.jpg"))


def clear_frames(media_id: str, frames_dir: Path = FRAMES_DIR) -> None:
    for path in existing_frames(media_id, frames_dir):
        path.unlink()


def extract_frames(record: MediaRecord, *, force: bool = False) -> list[Path]:
    source = Path(record.path)
    if not source.is_file():
        raise FileNotFoundError(f"Missing source media: {source}")

    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    already = existing_frames(record.id)
    if already and not force:
        return already

    clear_frames(record.id)
    output = frame_pattern(record.id)
    cmd = [
        ffmpeg_exe(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-vf",
        f"fps={SAMPLE_FPS},scale={SCALE_WIDTH}:-2",
        str(output),
    ]
    subprocess.run(cmd, check=True)
    return existing_frames(record.id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sample proxy frames from videos.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate frames even if they already exist.",
    )
    parser.add_argument(
        "--id",
        action="append",
        dest="ids",
        help="Only process these media ids (repeatable).",
    )
    args = parser.parse_args()

    try:
        videos = load_videos()
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    if args.ids:
        wanted = set(args.ids)
        videos = [v for v in videos if v.id in wanted]
        missing = wanted - {v.id for v in videos}
        if missing:
            print(f"Unknown or non-video ids: {', '.join(sorted(missing))}", file=sys.stderr)
            sys.exit(1)

    if not videos:
        print("No videos to process.")
        return

    total_frames = 0
    for index, record in enumerate(videos, start=1):
        print(f"[{index}/{len(videos)}] {record.id} ({record.duration or '?'}s) …", flush=True)
        try:
            frames = extract_frames(record, force=args.force)
        except subprocess.CalledProcessError as exc:
            print(f"  ffmpeg failed for {record.id}: {exc}", file=sys.stderr)
            sys.exit(1)
        total_frames += len(frames)
        print(f"  -> {len(frames)} frames")

    print(f"Done. {total_frames} frames in {FRAMES_DIR}")


if __name__ == "__main__":
    main()
