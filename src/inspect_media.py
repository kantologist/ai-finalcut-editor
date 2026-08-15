"""Walk workspace/originals/ and write a media inventory to metadata/media.json."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ExifTags
from pydantic import BaseModel, Field

from .ffmpeg import ffprobe_exe
from .paths import ROOT

ORIGINALS_DIR = ROOT / "workspace" / "originals"
METADATA_DIR = ROOT / "workspace" / "metadata"
OUTPUT_PATH = METADATA_DIR / "media.json"

VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".mts", ".m2ts", ".webm"}
IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".tif",
    ".tiff",
    ".webp",
    ".gif",
    ".bmp",
    ".dng",
}


class MediaRecord(BaseModel):
    id: str
    path: str
    type: Literal["video", "image"]
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    creation_time: str | None = None
    favorite: bool | None = Field(default=None)
    color_primaries: str | None = None
    color_transfer: str | None = None
    color_space: str | None = None


def _parse_rate(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        rate = float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None
    return round(rate, 3) if rate > 0 else None


def _normalize_creation_time(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    return text


def _ffprobe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            ffprobe_exe(),
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _clean_color_tag(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"unknown", "unspecified", "na", "n/a"}:
        return None
    return text


def _color_from_stream(stream: dict[str, Any] | None) -> dict[str, str | None]:
    if not stream:
        return {"color_primaries": None, "color_transfer": None, "color_space": None}
    transfer = stream.get("color_transfer") or stream.get("color_trc")
    return {
        "color_primaries": _clean_color_tag(stream.get("color_primaries")),
        "color_transfer": _clean_color_tag(transfer),
        "color_space": _clean_color_tag(stream.get("color_space") or stream.get("colorspace")),
    }


def _ffprobe_color(path: Path) -> dict[str, str | None]:
    try:
        probe = _ffprobe(path)
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return {"color_primaries": None, "color_transfer": None, "color_space": None}
    return _color_from_stream(_primary_video_stream(probe))


def _primary_video_stream(probe: dict[str, Any]) -> dict[str, Any] | None:
    streams = probe.get("streams") or []
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        return None
    # Prefer the default / non-dependent stream (avoids HEIC tile thumbnails).
    for stream in video_streams:
        disposition = stream.get("disposition") or {}
        if disposition.get("default") == 1:
            return stream
    for stream in video_streams:
        disposition = stream.get("disposition") or {}
        if disposition.get("dependent") != 1:
            return stream
    return max(video_streams, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0))


def _creation_time_from_probe(probe: dict[str, Any], stream: dict[str, Any] | None) -> str | None:
    candidates: list[str | None] = []
    if stream:
        tags = stream.get("tags") or {}
        candidates.append(tags.get("creation_time"))
    format_tags = (probe.get("format") or {}).get("tags") or {}
    candidates.extend(
        [
            format_tags.get("creation_time"),
            format_tags.get("com.apple.quicktime.creationdate"),
        ]
    )
    for value in candidates:
        normalized = _normalize_creation_time(value)
        if normalized:
            return normalized
    return None


def inspect_video(path: Path) -> MediaRecord:
    probe = _ffprobe(path)
    stream = _primary_video_stream(probe)
    fmt = probe.get("format") or {}

    duration = None
    if stream and stream.get("duration") is not None:
        duration = round(float(stream["duration"]), 2)
    elif fmt.get("duration") is not None:
        duration = round(float(fmt["duration"]), 2)

    fps = None
    if stream:
        # Prefer nominal r_frame_rate (e.g. 29.97) over irregular avg_frame_rate.
        fps = _parse_rate(stream.get("r_frame_rate")) or _parse_rate(stream.get("avg_frame_rate"))

    color = _color_from_stream(stream)
    return MediaRecord(
        id=path.stem,
        path=str(path.resolve()),
        type="video",
        duration=duration,
        width=stream.get("width") if stream else None,
        height=stream.get("height") if stream else None,
        fps=fps,
        creation_time=_creation_time_from_probe(probe, stream),
        favorite=None,
        color_primaries=color["color_primaries"],
        color_transfer=color["color_transfer"],
        color_space=color["color_space"],
    )


def _exiftool_image(path: Path) -> MediaRecord | None:
    try:
        result = subprocess.run(
            [
                "exiftool",
                "-json",
                "-n",
                "-ImageWidth",
                "-ImageHeight",
                "-DateTimeOriginal",
                "-CreateDate",
                "-MediaCreateDate",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    rows = json.loads(result.stdout)
    if not rows:
        return None
    row = rows[0]
    creation = (
        _normalize_creation_time(row.get("DateTimeOriginal"))
        or _normalize_creation_time(row.get("CreateDate"))
        or _normalize_creation_time(row.get("MediaCreateDate"))
    )
    color = _ffprobe_color(path)
    return MediaRecord(
        id=path.stem,
        path=str(path.resolve()),
        type="image",
        duration=None,
        width=row.get("ImageWidth"),
        height=row.get("ImageHeight"),
        fps=None,
        creation_time=creation,
        favorite=None,
        color_primaries=color["color_primaries"],
        color_transfer=color["color_transfer"],
        color_space=color["color_space"],
    )


def _pillow_creation_time(image: Image.Image) -> str | None:
    exif = image.getexif()
    if not exif:
        return None
    tag_map = {ExifTags.Base.DateTimeOriginal: None, ExifTags.Base.DateTime: None}
    for tag_id, value in exif.items():
        if tag_id in tag_map and isinstance(value, str):
            normalized = _normalize_creation_time(value)
            if normalized:
                return normalized
    return None


def inspect_image(path: Path) -> MediaRecord:
    # Prefer ExifTool for HEIC/HEIF and rich EXIF; fall back to Pillow.
    if path.suffix.lower() in {".heic", ".heif"}:
        record = _exiftool_image(path)
        if record is not None:
            return record

    try:
        with Image.open(path) as image:
            width, height = image.size
            creation_time = _pillow_creation_time(image)
        color = _ffprobe_color(path)
        return MediaRecord(
            id=path.stem,
            path=str(path.resolve()),
            type="image",
            duration=None,
            width=width,
            height=height,
            fps=None,
            creation_time=creation_time,
            favorite=None,
            color_primaries=color["color_primaries"],
            color_transfer=color["color_transfer"],
            color_space=color["color_space"],
        )
    except Exception:
        record = _exiftool_image(path)
        if record is not None:
            return record
        raise


def iter_media_files(directory: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() in VIDEO_EXTENSIONS | IMAGE_EXTENSIONS:
            files.append(path)
    return files


def inspect_media(directory: Path = ORIGINALS_DIR) -> list[MediaRecord]:
    records: list[MediaRecord] = []
    for path in iter_media_files(directory):
        suffix = path.suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            records.append(inspect_video(path))
        else:
            records.append(inspect_image(path))
    return records


def main() -> None:
    if not ORIGINALS_DIR.is_dir():
        print(f"Missing originals directory: {ORIGINALS_DIR}", file=sys.stderr)
        sys.exit(1)

    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    records = inspect_media(ORIGINALS_DIR)
    payload = [record.model_dump() for record in records]
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(records)} records to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
