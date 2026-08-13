"""Deterministic FCPXML generator from a validated Edit Decision List.

Architecture:
  EDL JSON → this program → FCPXML
(no LLM involvement)

Critical Final Cut import rules this generator follows:
- Keep rational times on the media/sequence timebase (do NOT reduce fractions —
  ``Fraction`` would turn ``120120/30000`` into ``1001/250``, which often breaks FCP).
- Never put still images on ``<asset-clip>`` (known FCP null-deref crash). Convert
  HEIC/JPEG stills into short H.264 MOV proxies first.
- Use a simple ``library → event → project → sequence → spine`` structure.
- Omit fragile extras (keywords with mixed timebases, invented format names).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from xml.dom import minidom

from .ffmpeg import ffmpeg_exe
from .paths import ROOT

METADATA_PATH = ROOT / "workspace" / "metadata" / "media.json"
DEFAULT_EDL_PATH = ROOT / "workspace" / "edits" / "edit.json"
DEFAULT_OUTPUT_PATH = ROOT / "workspace" / "output" / "lagos_v1.fcpxml"
STILLS_DIR = ROOT / "workspace" / "proxies" / "stills"

FCPXML_VERSION = "1.9"
EVENT_NAME = "AI Travel Editor"
PROJECT_NAME = "Lagos v1"

IMAGE_EXTENSIONS = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".gif", ".bmp"}

KNOWN_FRAME_DURATIONS: dict[float, Fraction] = {
    23.976: Fraction(1001, 24000),
    23.98: Fraction(1001, 24000),
    24.0: Fraction(1, 24),
    25.0: Fraction(1, 25),
    29.97: Fraction(1001, 30000),
    30.0: Fraction(1, 30),
    50.0: Fraction(1, 50),
    59.94: Fraction(1001, 60000),
    60.0: Fraction(1, 60),
}

DEFAULT_SEQUENCE_WIDTH = 1080
DEFAULT_SEQUENCE_HEIGHT = 1920
DEFAULT_SEQUENCE_FRAME_DURATION = Fraction(1001, 30000)


def active_sequence_size() -> tuple[int, int]:
    try:
        from .settings import load_settings

        cfg = load_settings()
        width = int(cfg.get("sequence_width", DEFAULT_SEQUENCE_WIDTH))
        height = int(cfg.get("sequence_height", DEFAULT_SEQUENCE_HEIGHT))
        if width < 16 or height < 16:
            return DEFAULT_SEQUENCE_WIDTH, DEFAULT_SEQUENCE_HEIGHT
        return width, height
    except Exception:  # noqa: BLE001
        return DEFAULT_SEQUENCE_WIDTH, DEFAULT_SEQUENCE_HEIGHT


def sequence_aspect_label(width: int, height: int) -> str:
    ratio = width / height if height else 0
    if ratio <= 0:
        return f"{width}x{height}"
    if ratio >= 2.3:
        return "ultrawide"
    if ratio >= 1.7:
        return "16:9"
    if ratio >= 1.5:
        return "3:2"
    if ratio >= 1.25:
        return "4:3"
    if ratio >= 0.9:
        return "1:1"
    if ratio >= 0.7:
        return "3:4"
    return "9:16"


def is_vertical_frame(src_w: int, src_h: int) -> bool:
    """Portrait or square source (height >= width)."""
    return src_h > 0 and src_w > 0 and src_h >= src_w


def fill_scale_for_aspect(
    src_w: int,
    src_h: int,
    seq_w: int | None = None,
    seq_h: int | None = None,
) -> float:
    """Zoom factor after Fit so the clip fills the sequence (Spatial Conform: Fill)."""
    if seq_w is None or seq_h is None:
        seq_w, seq_h = active_sequence_size()
    if src_w <= 0 or src_h <= 0 or seq_w <= 0 or seq_h <= 0:
        return 1.0
    src_ar = src_w / src_h
    seq_ar = seq_w / seq_h
    if abs(src_ar - seq_ar) < 1e-3:
        return 1.0
    return max(src_ar / seq_ar, seq_ar / src_ar)


def transform_scale_for_clip(
    src_w: int,
    src_h: int,
    *,
    seq_w: int,
    seq_h: int,
    mode: str,
) -> float:
    """Return adjust-transform scale (>1 ≈ Fill zoom). 1.0 leaves FCP default Fit."""
    if mode == "fit":
        return 1.0
    if mode == "fill":
        return fill_scale_for_aspect(src_w, src_h, seq_w, seq_h)
    # Default / fill_vertical_fit_wide: portrait Fill, landscape Fit.
    if is_vertical_frame(src_w, src_h):
        return fill_scale_for_aspect(src_w, src_h, seq_w, seq_h)
    return 1.0


def active_spatial_conform() -> str:
    try:
        from .settings import load_settings

        mode = str(
            load_settings().get("spatial_conform") or "fill_vertical_fit_wide"
        ).strip().lower()
        if mode in {"fill", "fit", "fill_vertical_fit_wide"}:
            return mode
        return "fill_vertical_fit_wide"
    except Exception:  # noqa: BLE001
        return "fill_vertical_fit_wide"


@dataclass(frozen=True)
class MediaInfo:
    asset_name: str
    path: Path
    media_type: str
    width: int
    height: int
    fps: float | None
    duration: float | None


@dataclass(frozen=True)
class EdlClip:
    asset: str
    source_start: float | None
    source_duration: float
    reason: str


@dataclass(frozen=True)
class Timebase:
    frame_duration: Fraction

    @property
    def ticks_per_frame(self) -> int:
        return int(self.frame_duration.numerator)

    @property
    def timescale(self) -> int:
        return int(self.frame_duration.denominator)


# ---------------------------------------------------------------------------
# Rational time — frame counts + unreduced serialization
# ---------------------------------------------------------------------------


def frame_duration_for_fps(fps: float | None) -> Fraction:
    if fps is None or fps <= 0:
        return DEFAULT_SEQUENCE_FRAME_DURATION
    for known, dur in KNOWN_FRAME_DURATIONS.items():
        if abs(known - float(fps)) < 0.02:
            return dur
    if abs(fps - round(fps)) < 1e-6:
        return Fraction(1, int(round(fps)))
    denom = int(round(float(fps) * 1001))
    return Fraction(1001, max(denom, 1))


def seconds_to_frames(seconds: float, timebase: Timebase) -> int:
    if seconds <= 0:
        return 0
    return max(0, int(round(seconds / float(timebase.frame_duration))))


def format_frames(frames: int, timebase: Timebase) -> str:
    """Serialize frame count as FCPXML time, keeping the native timescale."""
    if frames <= 0:
        return "0s"
    num = frames * timebase.ticks_per_frame
    den = timebase.timescale
    # Intentionally unreduced (e.g. 120120/30000s, not 1001/250s).
    return f"{num}/{den}s"


def file_url(path: Path) -> str:
    return path.resolve().as_uri()


def stable_uid(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts)))


# ---------------------------------------------------------------------------
# Loaders / still proxies
# ---------------------------------------------------------------------------


def load_media(metadata_path: Path = METADATA_PATH) -> dict[str, MediaInfo]:
    if not metadata_path.is_file():
        raise FileNotFoundError(f"Missing media inventory: {metadata_path}")
    rows = json.loads(metadata_path.read_text(encoding="utf-8"))
    by_name: dict[str, MediaInfo] = {}
    for row in rows:
        path = Path(row["path"])
        by_name[path.name] = MediaInfo(
            asset_name=path.name,
            path=path,
            media_type=str(row.get("type") or "video"),
            width=int(row.get("width") or DEFAULT_SEQUENCE_WIDTH),
            height=int(row.get("height") or DEFAULT_SEQUENCE_HEIGHT),
            fps=(float(row["fps"]) if row.get("fps") is not None else None),
            duration=(float(row["duration"]) if row.get("duration") is not None else None),
        )
    return by_name


def load_edl(path: Path) -> list[EdlClip]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing EDL: {path}. Run plan_edit.py first.")
    data = json.loads(path.read_text(encoding="utf-8"))
    clips = [
        EdlClip(
            asset=str(row["asset"]),
            source_start=(None if row.get("source_start") is None else float(row["source_start"])),
            source_duration=float(row["source_duration"]),
            reason=str(row.get("reason") or ""),
        )
        for row in (data.get("timeline") or [])
    ]
    if not clips:
        raise ValueError(f"EDL has empty timeline: {path}")
    return clips


def is_still(media: MediaInfo) -> bool:
    return media.media_type == "image" or media.path.suffix.lower() in IMAGE_EXTENSIONS


def _sips_jpeg(source: Path, out: Path) -> tuple[int, int]:
    subprocess.run(
        ["sips", "-Z", "1920", "-s", "format", "jpeg", str(source), "--out", str(out)],
        check=True,
        capture_output=True,
    )
    if not out.is_file() or out.stat().st_size < 1000:
        raise RuntimeError(f"Failed to build JPEG from {source.name}")
    probe = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(out)],
        check=True,
        capture_output=True,
        text=True,
    )
    width = 1920
    height = 1080
    for line in probe.stdout.splitlines():
        if "pixelWidth:" in line:
            width = int(line.split(":")[-1].strip())
        elif "pixelHeight:" in line:
            height = int(line.split(":")[-1].strip())
    return width, height


def ensure_still_video(media: MediaInfo, *, hold_seconds: float = 10.0) -> tuple[Path, int, int, float]:
    """
    FCP crashes when still images (JPEG/HEIC/PNG) are referenced via <asset-clip>.
    Convert each still into a short H.264 MOV so the spine is video-only.
    Returns (mov_path, width, height, duration_seconds).
    """
    STILLS_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(media.asset_name).stem
    jpg = STILLS_DIR / f"{stem}.jpg"
    mov = STILLS_DIR / f"{stem}.mov"
    width, height = _sips_jpeg(media.path, jpg)

    # Even dimensions required by yuv420p.
    width -= width % 2
    height -= height % 2
    duration = max(hold_seconds, 4.0)

    needs_build = (
        not mov.is_file()
        or mov.stat().st_size < 1000
        or mov.stat().st_mtime < jpg.stat().st_mtime
    )
    if needs_build:
        # 29.97-compatible integer 30 fps keeps the still timebase simple.
        cmd = [
            ffmpeg_exe(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-i",
            str(jpg),
            "-t",
            f"{duration:.3f}",
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            f"scale={width}:{height}",
            "-an",
            str(mov),
        ]
        subprocess.run(cmd, check=True)
        if not mov.is_file() or mov.stat().st_size < 1000:
            raise RuntimeError(f"Failed to build still MOV for {media.asset_name}")

    return mov, width, height, duration


def choose_sequence_format(used: list[MediaInfo]) -> tuple[int, int, Timebase]:
    """Sequence size from settings (default vertical 9:16 1080×1920 @ 29.97)."""
    width, height = active_sequence_size()
    return width, height, Timebase(DEFAULT_SEQUENCE_FRAME_DURATION)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_fcpxml(
    clips: list[EdlClip],
    media_by_name: dict[str, MediaInfo],
    *,
    event_name: str = EVENT_NAME,
    project_name: str = PROJECT_NAME,
) -> ET.Element:
    used_names: list[str] = []
    for clip in clips:
        if clip.asset not in media_by_name:
            raise KeyError(f"EDL asset not in media inventory: {clip.asset}")
        if clip.asset not in used_names:
            used_names.append(clip.asset)
    used_media = [media_by_name[name] for name in used_names]

    seq_w, seq_h, seq_tb = choose_sequence_format(used_media)

    root = ET.Element("fcpxml", {"version": FCPXML_VERSION})
    resources = ET.SubElement(root, "resources")

    next_id = 1

    def alloc_id() -> str:
        nonlocal next_id
        rid = f"r{next_id}"
        next_id += 1
        return rid

    # Sequence format: custom width/height (vertical 9:16 by default).
    # Avoid landscape-only predefined names like FFVideoFormat1080p2997.
    seq_format_id = alloc_id()
    seq_format_attrs = {
        "id": seq_format_id,
        "frameDuration": format_frames(1, seq_tb),
        "width": str(seq_w),
        "height": str(seq_h),
    }
    if seq_w == 1920 and seq_h == 1080:
        seq_format_attrs["name"] = "FFVideoFormat1080p2997"
    ET.SubElement(resources, "format", seq_format_attrs)

    asset_ids: dict[str, str] = {}
    asset_timebases: dict[str, Timebase] = {}
    asset_durations_frames: dict[str, int] = {}
    asset_dimensions: dict[str, tuple[int, int]] = {}
    format_ids: dict[tuple[int, int, int, int], str] = {}  # w,h,ticks,timescale
    spatial_conform = active_spatial_conform()

    for media in used_media:
        if is_still(media):
            # CRITICAL: never put JPEG/HEIC on an <asset-clip> — FCP null-derefs.
            media_path, width, height, still_dur = ensure_still_video(media)
            tb = Timebase(Fraction(1, 30))
            has_audio = False
            total_frames = seconds_to_frames(still_dur, tb)
            asset_duration_attr = format_frames(total_frames, tb)
            asset_durations_frames[media.asset_name] = total_frames
        else:
            media_path = media.path
            width, height = media.width, media.height
            tb = Timebase(frame_duration_for_fps(media.fps))
            has_audio = True
            total_frames = seconds_to_frames(media.duration or 0.0, tb)
            if total_frames <= 0:
                total_frames = 1
            asset_duration_attr = format_frames(total_frames, tb)
            asset_durations_frames[media.asset_name] = total_frames

        asset_timebases[media.asset_name] = tb
        asset_dimensions[media.asset_name] = (width, height)

        fmt_key = (width, height, tb.ticks_per_frame, tb.timescale)
        if fmt_key not in format_ids:
            fid = alloc_id()
            format_ids[fmt_key] = fid
            ET.SubElement(
                resources,
                "format",
                {
                    "id": fid,
                    "frameDuration": format_frames(1, tb),
                    "width": str(width),
                    "height": str(height),
                },
            )

        aid = alloc_id()
        asset_ids[media.asset_name] = aid
        attrs = {
            "id": aid,
            "name": media.asset_name,
            "uid": stable_uid("asset", str(media_path.resolve())),
            "start": "0s",
            "duration": asset_duration_attr,
            "hasVideo": "1",
            "hasAudio": "1" if has_audio else "0",
            "format": format_ids[fmt_key],
        }
        if has_audio:
            attrs["audioSources"] = "1"
            attrs["audioChannels"] = "2"
            attrs["audioRate"] = "48000"
        asset_el = ET.SubElement(resources, "asset", attrs)
        ET.SubElement(
            asset_el,
            "media-rep",
            {"kind": "original-media", "src": file_url(media_path)},
        )

    # Import-friendly: library → event → project (no library @location required).
    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", {"name": event_name})
    project = ET.SubElement(event, "project", {"name": project_name})

    spine_specs: list[tuple[dict[str, str], float]] = []
    timeline_frames = 0

    for clip in clips:
        media = media_by_name[clip.asset]
        src_tb = asset_timebases[clip.asset]
        tl_frames = max(1, seconds_to_frames(clip.source_duration, seq_tb))

        attrs: dict[str, str] = {
            "ref": asset_ids[clip.asset],
            "offset": format_frames(timeline_frames, seq_tb),
            "name": media.asset_name,
            "duration": format_frames(tl_frames, seq_tb),
            "tcFormat": "NDF",
        }

        start_frames = 0 if is_still(media) else seconds_to_frames(clip.source_start or 0.0, src_tb)
        max_frames = asset_durations_frames[clip.asset]
        src_need = max(1, seconds_to_frames(clip.source_duration, src_tb))
        if start_frames + src_need > max_frames:
            start_frames = max(0, max_frames - src_need)
        attrs["start"] = format_frames(start_frames, src_tb)

        src_w, src_h = asset_dimensions[clip.asset]
        scale = transform_scale_for_clip(
            src_w,
            src_h,
            seq_w=seq_w,
            seq_h=seq_h,
            mode=spatial_conform,
        )
        spine_specs.append((attrs, scale))
        timeline_frames += tl_frames

    sequence = ET.SubElement(
        project,
        "sequence",
        {
            "format": seq_format_id,
            "duration": format_frames(timeline_frames, seq_tb),
            "tcStart": "0s",
            "tcFormat": "NDF",
            "audioLayout": "stereo",
            "audioRate": "48k",
        },
    )
    spine = ET.SubElement(sequence, "spine")
    for attrs, scale in spine_specs:
        clip_el = ET.SubElement(spine, "asset-clip", attrs)
        # FCP defaults to Fit. Scale > 1 ≈ Fill zoom for portrait sources.
        # Wide (landscape) clips stay at 1.0 under fill_vertical_fit_wide.
        if scale > 1.001:
            scale_s = f"{scale:.6f}".rstrip("0").rstrip(".")
            ET.SubElement(
                clip_el,
                "adjust-transform",
                {"scale": f"{scale_s} {scale_s}"},
            )

    return root


def tostring(root: ET.Element) -> str:
    rough = ET.tostring(root, encoding="utf-8")
    parsed = minidom.parseString(rough)
    pretty = parsed.toprettyxml(indent="  ", encoding="UTF-8").decode("utf-8")
    declaration = '<?xml version="1.0" encoding="UTF-8"?>'
    doctype = "<!DOCTYPE fcpxml>"
    lines = pretty.splitlines()
    body = "\n".join(lines[1:]).lstrip("\n") if lines and lines[0].startswith("<?xml") else pretty
    # Drop blank lines minidom inserts.
    cleaned = "\n".join(line for line in body.splitlines() if line.strip())
    return f"{declaration}\n{doctype}\n{cleaned}\n"


def write_fcpxml(root: ET.Element, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tostring(root), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deterministically translate an EDL into Final Cut Pro XML."
    )
    parser.add_argument("--edl", type=Path, default=DEFAULT_EDL_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--event-name", default=EVENT_NAME)
    parser.add_argument("--project-name", default=PROJECT_NAME)
    args = parser.parse_args()

    try:
        clips = load_edl(args.edl)
        media = load_media()
        root = build_fcpxml(
            clips,
            media,
            event_name=args.event_name,
            project_name=args.project_name,
        )
        path = write_fcpxml(root, args.output)
    except Exception as exc:  # noqa: BLE001
        print(f"failed: {exc}", file=sys.stderr)
        sys.exit(1)

    seq = root.find("./library/event/project/sequence")
    duration = seq.get("duration") if seq is not None else "?"
    spine_count = len(root.findall(".//spine/asset-clip"))
    print(f"Wrote {spine_count} clips ({duration}) → {path}")
    print("Import in Final Cut: File → Import → XML… (into a new or existing library)")


if __name__ == "__main__":
    main()
