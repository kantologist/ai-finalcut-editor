"""Analyze sampled proxy frames with a vision model (structured JSON output).

Vision estimates creative signals. Stability is measured locally (Laplacian
sharpness for now; optical-flow shake later). Final segment_score is a
weighted blend computed in code — not by the model.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from openai import APIStatusError, OpenAI, RateLimitError
from PIL import Image, ImageFilter, ImageStat
from pydantic import BaseModel, Field

from .paths import ROOT

METADATA_PATH = ROOT / "workspace" / "metadata" / "media.json"
FRAMES_DIR = ROOT / "workspace" / "frames"
ANALYSIS_DIR = ROOT / "workspace" / "analysis"
PROMPT_PATH = ROOT / "prompts" / "vision.md"

# Must match make_proxies sampling (fps=1/2).
FRAME_INTERVAL_SEC = 2.0
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_BATCH_SIZE = 8
DEFAULT_MAX_RETRIES = 12
DEFAULT_REQUEST_PAUSE_SEC = 1.0

SCORE_WEIGHTS = {
    "visual_interest": 0.30,
    "composition": 0.20,
    "stability": 0.15,
    "story_value": 0.15,
    "uniqueness": 0.10,
    "user_priority": 0.10,
}


def active_score_weights() -> dict[str, float]:
    try:
        from .settings import load_settings

        weights = load_settings().get("score_weights") or SCORE_WEIGHTS
        return {key: float(weights.get(key, SCORE_WEIGHTS[key])) for key in SCORE_WEIGHTS}
    except Exception:  # noqa: BLE001
        return dict(SCORE_WEIGHTS)

# Soft reference for absolute Laplacian variance on ~768px proxies.
# Per-clip normalization still dominates ranking within a video.
SHARPNESS_REF = 400.0

RETRY_HINT_RE = re.compile(
    r"try again in\s+(?P<amount>[\d.]+)\s*(?P<unit>ms|s|m|minutes?)?",
    re.IGNORECASE,
)
FRAME_RE = re.compile(r"^(?P<id>.+)_(?P<index>\d{4})\.jpg$", re.IGNORECASE)

ShotType = Literal["wide", "medium", "closeup", "detail", "aerial", "other"]
CameraMotion = Literal[
    "static",
    "slow_pan",
    "pan",
    "tilt",
    "zoom",
    "handheld",
    "tracking",
    "drone",
    "other",
]


class MediaRecord(BaseModel):
    id: str
    path: str
    type: str
    duration: float | None = None
    favorite: bool | None = None


class FrameRef(BaseModel):
    path: Path
    index: int  # 1-based ffmpeg output index
    timestamp: float


class VisionSegment(BaseModel):
    """Fields produced by the vision model (no technical stability)."""

    start: float = Field(description="Segment start time in seconds")
    end: float = Field(description="Segment end time in seconds")
    description: str
    subjects: list[str]
    shot_type: ShotType
    camera_motion: CameraMotion
    visual_interest: float = Field(ge=0.0, le=1.0)
    composition: float = Field(ge=0.0, le=1.0)
    story_value: float = Field(ge=0.0, le=1.0)
    uniqueness: float = Field(ge=0.0, le=1.0)
    recommended: bool


class SegmentAnalysis(BaseModel):
    segments: list[VisionSegment]


class StillVisionResult(BaseModel):
    """Single still-image analysis (no timeline)."""

    description: str
    subjects: list[str]
    shot_type: ShotType
    camera_motion: CameraMotion = "static"
    visual_interest: float = Field(ge=0.0, le=1.0)
    composition: float = Field(ge=0.0, le=1.0)
    story_value: float = Field(ge=0.0, le=1.0)
    uniqueness: float = Field(ge=0.0, le=1.0)
    recommended: bool


class Segment(BaseModel):
    """Vision fields + locally derived scores."""

    start: float
    end: float
    description: str
    subjects: list[str]
    shot_type: ShotType
    camera_motion: CameraMotion
    visual_interest: float = Field(ge=0.0, le=1.0)
    composition: float = Field(ge=0.0, le=1.0)
    story_value: float = Field(ge=0.0, le=1.0)
    uniqueness: float = Field(ge=0.0, le=1.0)
    stability: float = Field(ge=0.0, le=1.0)
    user_priority: float = Field(ge=0.0, le=1.0)
    segment_score: float = Field(ge=0.0, le=1.0)
    recommended: bool


class ClipAnalysis(BaseModel):
    id: str
    source_path: str
    duration: float | None = None
    model: str
    frame_count: int
    score_weights: dict[str, float] = Field(default_factory=lambda: dict(SCORE_WEIGHTS))
    segments: list[Segment]


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def load_media(metadata_path: Path = METADATA_PATH) -> list[MediaRecord]:
    if not metadata_path.is_file():
        raise FileNotFoundError(
            f"Missing media inventory: {metadata_path}. Run inspect_media.py first."
        )
    return [MediaRecord.model_validate(row) for row in json.loads(metadata_path.read_text())]


def load_videos(metadata_path: Path = METADATA_PATH) -> list[MediaRecord]:
    return [r for r in load_media(metadata_path) if r.type == "video"]


def load_images(metadata_path: Path = METADATA_PATH) -> list[MediaRecord]:
    return [r for r in load_media(metadata_path) if r.type == "image"]


def frames_for_media(media_id: str, frames_dir: Path = FRAMES_DIR) -> list[FrameRef]:
    frames: list[FrameRef] = []
    for path in sorted(frames_dir.glob(f"{media_id}_*.jpg")):
        match = FRAME_RE.match(path.name)
        if not match or match.group("id") != media_id:
            continue
        index = int(match.group("index"))
        frames.append(
            FrameRef(
                path=path,
                index=index,
                timestamp=round((index - 1) * FRAME_INTERVAL_SEC, 2),
            )
        )
    return frames


def image_data_url(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if mime is None:
        mime = "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def batched(items: list[FrameRef], size: int) -> list[list[FrameRef]]:
    if size < 1:
        raise ValueError("batch size must be >= 1")
    return [items[i : i + size] for i in range(0, len(items), size)]


def user_priority_score(favorite: bool | None) -> float:
    if favorite is True:
        return 1.0
    if favorite is False:
        return 0.0
    return 0.5


def laplacian_variance(path: Path) -> float:
    """Focus/blur proxy via variance of a Laplacian-filtered grayscale frame."""
    with Image.open(path) as image:
        gray = image.convert("L")
    # 3x3 Laplacian kernel.
    kernel = ImageFilter.Kernel(
        (3, 3),
        [-1, -1, -1, -1, 8, -1, -1, -1, -1],
        scale=1,
        offset=128,
    )
    filtered = gray.filter(kernel)
    return float(ImageStat.Stat(filtered).var[0])


def sharpness_map(frames: list[FrameRef]) -> dict[int, float]:
    return {frame.index: laplacian_variance(frame.path) for frame in frames}


def normalize_stability(raw_values: list[float]) -> list[float]:
    """Map sharpness to 0–1 using clip-relative range with an absolute floor."""
    if not raw_values:
        return []
    absolute = [max(0.0, min(1.0, value / SHARPNESS_REF)) for value in raw_values]
    lo = min(raw_values)
    hi = max(raw_values)
    if hi - lo < 1e-6:
        return absolute
    relative = [(value - lo) / (hi - lo) for value in raw_values]
    # Blend absolute focus with within-clip ranking.
    return [round(0.5 * a + 0.5 * r, 4) for a, r in zip(absolute, relative)]


def frames_covering_segment(frames: list[FrameRef], start: float, end: float) -> list[FrameRef]:
    covering = [f for f in frames if start - 1e-6 <= f.timestamp <= end + 1e-6]
    if covering:
        return covering
    # Fallback: nearest frame to the segment midpoint.
    mid = (start + end) / 2.0
    nearest = min(frames, key=lambda f: abs(f.timestamp - mid))
    return [nearest]


def stability_for_segment(
    frames: list[FrameRef],
    *,
    start: float,
    end: float,
    stability_by_index: dict[int, float],
) -> float:
    covering = frames_covering_segment(frames, start, end)
    values = [stability_by_index[f.index] for f in covering]
    return round(sum(values) / len(values), 4)


def compute_segment_score(
    *,
    visual_interest: float,
    composition: float,
    stability: float,
    story_value: float,
    uniqueness: float,
    user_priority: float,
) -> float:
    weights = active_score_weights()
    score = (
        weights["visual_interest"] * visual_interest
        + weights["composition"] * composition
        + weights["stability"] * stability
        + weights["story_value"] * story_value
        + weights["uniqueness"] * uniqueness
        + weights["user_priority"] * user_priority
    )
    return round(max(0.0, min(1.0, score)), 4)


def enrich_segments(
    vision_segments: list[VisionSegment],
    *,
    frames: list[FrameRef],
    favorite: bool | None,
) -> list[Segment]:
    raw_sharpness = sharpness_map(frames)
    ordered_indexes = [f.index for f in frames]
    normalized = normalize_stability([raw_sharpness[i] for i in ordered_indexes])
    stability_by_index = dict(zip(ordered_indexes, normalized))
    priority = user_priority_score(favorite)

    enriched: list[Segment] = []
    for item in vision_segments:
        stability = stability_for_segment(
            frames,
            start=item.start,
            end=item.end,
            stability_by_index=stability_by_index,
        )
        enriched.append(
            Segment(
                start=item.start,
                end=item.end,
                description=item.description,
                subjects=item.subjects,
                shot_type=item.shot_type,
                camera_motion=item.camera_motion,
                visual_interest=item.visual_interest,
                composition=item.composition,
                story_value=item.story_value,
                uniqueness=item.uniqueness,
                stability=stability,
                user_priority=priority,
                segment_score=compute_segment_score(
                    visual_interest=item.visual_interest,
                    composition=item.composition,
                    stability=stability,
                    story_value=item.story_value,
                    uniqueness=item.uniqueness,
                    user_priority=priority,
                ),
                recommended=item.recommended,
            )
        )
    return enriched


def vision_segment_from_legacy(row: dict) -> VisionSegment:
    """Best-effort map from the previous score schema."""
    if "visual_interest" in row:
        return VisionSegment.model_validate(row)

    aesthetic = float(row.get("aesthetic_score", 0.5))
    story = float(row.get("story_score", 0.5))
    duplicate = float(row.get("duplicate_likelihood", 0.0))
    return VisionSegment(
        start=float(row["start"]),
        end=float(row["end"]),
        description=str(row.get("description", "")),
        subjects=list(row.get("subjects") or []),
        shot_type=row.get("shot_type") or "other",
        camera_motion=row.get("camera_motion") or "other",
        visual_interest=aesthetic,
        composition=aesthetic,
        story_value=story,
        uniqueness=max(0.0, min(1.0, 1.0 - duplicate)),
        recommended=bool(row.get("recommended", False)),
    )


def retry_after_seconds(exc: Exception, attempt: int) -> float:
    """Prefer server-provided wait time; otherwise exponential backoff."""
    headers = getattr(exc, "response", None)
    header_value = None
    if headers is not None:
        header_value = headers.headers.get("retry-after") or headers.headers.get(
            "x-ratelimit-reset-tokens"
        )
    if header_value:
        try:
            return max(float(header_value), 0.5)
        except ValueError:
            pass

    message = str(exc)
    match = RETRY_HINT_RE.search(message)
    if match:
        amount = float(match.group("amount"))
        unit = (match.group("unit") or "s").lower()
        if unit.startswith("ms"):
            return max(amount / 1000.0, 0.5)
        if unit.startswith("m"):
            return max(amount * 60.0, 0.5)
        return max(amount, 0.5)

    return min(2**attempt, 60) + random.uniform(0.1, 0.5)


def with_rate_limit_retries(fn, *, max_retries: int, label: str):
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except RateLimitError as exc:
            last_exc = exc
            if attempt >= max_retries:
                break
            wait = retry_after_seconds(exc, attempt)
            print(
                f"  rate limited on {label}; sleeping {wait:.1f}s "
                f"(attempt {attempt + 1}/{max_retries})",
                flush=True,
            )
            time.sleep(wait)
        except APIStatusError as exc:
            if exc.status_code != 429:
                raise
            last_exc = exc
            if attempt >= max_retries:
                break
            wait = retry_after_seconds(exc, attempt)
            print(
                f"  rate limited on {label}; sleeping {wait:.1f}s "
                f"(attempt {attempt + 1}/{max_retries})",
                flush=True,
            )
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def analyze_frame_batch(
    client: OpenAI,
    *,
    media: MediaRecord,
    frames: list[FrameRef],
    model: str,
    system_prompt: str,
    max_retries: int,
) -> SegmentAnalysis:
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                f"Clip id: {media.id}\n"
                f"Clip duration: {media.duration if media.duration is not None else 'unknown'} seconds\n"
                f"Frame interval: {FRAME_INTERVAL_SEC} seconds\n"
                f"You are analyzing {len(frames)} sequential frames from this clip.\n"
                "Each image is labeled with its timestamp. Group them into segments.\n"
                "Do not score technical stability, shake, or blur."
            ),
        }
    ]
    for frame in frames:
        content.append({"type": "text", "text": f"Frame {frame.index:04d} @ {frame.timestamp:.2f}s"})
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": image_data_url(frame.path), "detail": "low"},
            }
        )

    def _call() -> SegmentAnalysis:
        completion = client.chat.completions.parse(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            response_format=SegmentAnalysis,
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError(f"Model returned no parsed content for {media.id}")
        return parsed

    label = f"{media.id} frames {frames[0].index:04d}-{frames[-1].index:04d}"
    return with_rate_limit_retries(_call, max_retries=max_retries, label=label)


def clamp_vision_segments(
    segments: list[VisionSegment],
    *,
    duration: float | None,
    batch_start: float,
    batch_end: float,
) -> list[VisionSegment]:
    clipped: list[VisionSegment] = []
    hard_end = duration if duration is not None else batch_end + FRAME_INTERVAL_SEC
    for segment in segments:
        start = max(batch_start, min(segment.start, hard_end))
        end = max(start, min(segment.end, hard_end))
        if end <= start:
            end = min(hard_end, start + FRAME_INTERVAL_SEC)
        clipped.append(segment.model_copy(update={"start": round(start, 2), "end": round(end, 2)}))
    return clipped


def analyze_clip(
    client: OpenAI,
    media: MediaRecord,
    *,
    model: str,
    batch_size: int,
    system_prompt: str,
    max_retries: int,
    request_pause_sec: float,
) -> ClipAnalysis:
    frames = frames_for_media(media.id)
    if not frames:
        raise FileNotFoundError(
            f"No frames for {media.id} in {FRAMES_DIR}. Run make_proxies.py first."
        )

    vision_segments: list[VisionSegment] = []
    batches = batched(frames, batch_size)
    for batch_index, batch in enumerate(batches):
        if batch_index > 0 and request_pause_sec > 0:
            time.sleep(request_pause_sec)
        result = analyze_frame_batch(
            client,
            media=media,
            frames=batch,
            model=model,
            system_prompt=system_prompt,
            max_retries=max_retries,
        )
        vision_segments.extend(
            clamp_vision_segments(
                result.segments,
                duration=media.duration,
                batch_start=batch[0].timestamp,
                batch_end=batch[-1].timestamp,
            )
        )

    vision_segments.sort(key=lambda s: (s.start, s.end))
    segments = enrich_segments(vision_segments, frames=frames, favorite=media.favorite)
    return ClipAnalysis(
        id=media.id,
        source_path=media.path,
        duration=media.duration,
        model=model,
        frame_count=len(frames),
        score_weights=active_score_weights(),
        segments=segments,
    )


def ensure_still_proxy(media: MediaRecord, *, force: bool = False) -> FrameRef:
    """Create a single JPEG proxy under workspace/frames for a still image."""
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    out = FRAMES_DIR / f"{media.id}_0001.jpg"
    if out.exists() and not force and out.stat().st_size > 20_000:
        return FrameRef(path=out, index=1, timestamp=0.0)

    source = Path(media.path)
    if not source.is_file():
        raise FileNotFoundError(f"Missing source image: {source}")

    suffix = source.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        with Image.open(source) as image:
            image = image.convert("RGB")
            image.thumbnail((768, 768))
            image.save(out, format="JPEG", quality=85)
    else:
        # HEIC/HEIF and other formats: prefer sips on macOS.
        import subprocess

        cmd = [
            "sips",
            "-Z",
            "768",
            "-s",
            "format",
            "jpeg",
            str(source),
            "--out",
            str(out),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        if not out.is_file() or out.stat().st_size < 1000:
            raise RuntimeError(f"Failed to build still proxy for {media.id}")

    return FrameRef(path=out, index=1, timestamp=0.0)


def analyze_still(
    client: OpenAI,
    media: MediaRecord,
    *,
    model: str,
    system_prompt: str,
    max_retries: int,
) -> ClipAnalysis:
    frame = ensure_still_proxy(media)
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                f"Still image id: {media.id}\n"
                "Analyze this single photograph as one candidate shot.\n"
                "Do not score technical stability, shake, or blur."
            ),
        },
        {"type": "image_url", "image_url": {"url": image_data_url(frame.path), "detail": "low"}},
    ]

    def _call() -> StillVisionResult:
        completion = client.chat.completions.parse(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            response_format=StillVisionResult,
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError(f"Model returned no parsed content for {media.id}")
        return parsed

    still = with_rate_limit_retries(_call, max_retries=max_retries, label=media.id)
    vision = VisionSegment(
        start=0.0,
        end=0.0,
        description=still.description,
        subjects=still.subjects,
        shot_type=still.shot_type,
        camera_motion=still.camera_motion,
        visual_interest=still.visual_interest,
        composition=still.composition,
        story_value=still.story_value,
        uniqueness=still.uniqueness,
        recommended=still.recommended,
    )
    segments = enrich_segments([vision], frames=[frame], favorite=media.favorite)
    return ClipAnalysis(
        id=media.id,
        source_path=media.path,
        duration=None,
        model=model,
        frame_count=1,
        score_weights=active_score_weights(),
        segments=segments,
    )


def rescore_existing(media: MediaRecord) -> ClipAnalysis:
    path = ANALYSIS_DIR / f"{media.id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"Missing analysis file: {path}")

    frames = frames_for_media(media.id)
    if not frames:
        raise FileNotFoundError(f"No frames for {media.id} in {FRAMES_DIR}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    vision_segments = [vision_segment_from_legacy(row) for row in raw.get("segments", [])]
    segments = enrich_segments(vision_segments, frames=frames, favorite=media.favorite)
    return ClipAnalysis(
        id=media.id,
        source_path=media.path,
        duration=media.duration,
        model=str(raw.get("model", "rescore-only")),
        frame_count=len(frames),
        score_weights=active_score_weights(),
        segments=segments,
    )


def write_analysis(analysis: ClipAnalysis, analysis_dir: Path = ANALYSIS_DIR) -> Path:
    analysis_dir.mkdir(parents=True, exist_ok=True)
    path = analysis_dir / f"{analysis.id}.json"
    path.write_text(analysis.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze proxy frames with a vision model.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"OpenAI model (default: {DEFAULT_MODEL})")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Frames per API call (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--id",
        action="append",
        dest="ids",
        help="Only analyze these media ids (repeatable).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-analyze even if analysis JSON already exists.",
    )
    parser.add_argument(
        "--rescore-only",
        action="store_true",
        help="Recompute local stability + segment_score from existing analysis (no API calls).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Analyze at most N clips (useful for smoke tests).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Retries on 429 rate limits (default: {DEFAULT_MAX_RETRIES}).",
    )
    parser.add_argument(
        "--request-pause",
        type=float,
        default=DEFAULT_REQUEST_PAUSE_SEC,
        help=f"Seconds to wait between batch requests (default: {DEFAULT_REQUEST_PAUSE_SEC}).",
    )
    parser.add_argument(
        "--images-only",
        action="store_true",
        help="Analyze still images only.",
    )
    parser.add_argument(
        "--videos-only",
        action="store_true",
        help="Analyze videos only (default includes both when needed).",
    )
    args = parser.parse_args()

    try:
        videos = load_videos()
        images = load_images()
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    if args.images_only and args.videos_only:
        print("Choose at most one of --images-only / --videos-only", file=sys.stderr)
        sys.exit(1)

    if args.images_only:
        targets = images
    elif args.videos_only:
        targets = videos
    else:
        targets = videos + images

    if args.ids:
        wanted = set(args.ids)
        targets = [v for v in targets if v.id in wanted]
        missing = wanted - {v.id for v in targets}
        if missing:
            print(f"Unknown media ids: {', '.join(sorted(missing))}", file=sys.stderr)
            sys.exit(1)

    if args.limit is not None:
        targets = targets[: args.limit]

    if not targets:
        print("No media to analyze.")
        return

    if args.rescore_only:
        written = 0
        for index, media in enumerate(targets, start=1):
            print(f"[{index}/{len(targets)}] rescore {media.id} …", flush=True)
            try:
                if media.type == "image":
                    ensure_still_proxy(media)
                analysis = rescore_existing(media)
            except Exception as exc:  # noqa: BLE001
                print(f"  failed: {exc}", file=sys.stderr)
                sys.exit(1)
            path = write_analysis(analysis)
            written += 1
            top = max((s.segment_score for s in analysis.segments), default=0.0)
            print(f"  -> {len(analysis.segments)} segments (top score {top:.2f}) → {path.name}")
        print(f"Done. Rescored {written} analysis files in {ANALYSIS_DIR}")
        return

    load_dotenv(ROOT / ".env")
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("OPENAI_API_KEY is missing. Add it to .env", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(api_key=api_key, max_retries=0)
    system_prompt = load_prompt()
    written = 0

    for index, media in enumerate(targets, start=1):
        out_path = ANALYSIS_DIR / f"{media.id}.json"
        if out_path.exists() and not args.force:
            print(f"[{index}/{len(targets)}] {media.id} — skip (exists)")
            continue

        kind = media.type
        print(
            f"[{index}/{len(targets)}] {media.id} "
            f"({kind}, {media.duration or 'still'}, batch={args.batch_size}) …",
            flush=True,
        )
        try:
            if media.type == "image":
                analysis = analyze_still(
                    client,
                    media,
                    model=args.model,
                    system_prompt=system_prompt,
                    max_retries=args.max_retries,
                )
            else:
                analysis = analyze_clip(
                    client,
                    media,
                    model=args.model,
                    batch_size=args.batch_size,
                    system_prompt=system_prompt,
                    max_retries=args.max_retries,
                    request_pause_sec=args.request_pause,
                )
        except Exception as exc:  # noqa: BLE001
            print(f"  failed: {exc}", file=sys.stderr)
            sys.exit(1)

        path = write_analysis(analysis)
        written += 1
        recommended = sum(1 for s in analysis.segments if s.recommended)
        top = max((s.segment_score for s in analysis.segments), default=0.0)
        print(
            f"  -> {len(analysis.segments)} segments "
            f"({recommended} recommended, top score {top:.2f}) → {path.name}"
        )
        if args.request_pause > 0:
            time.sleep(args.request_pause)

    print(f"Done. Wrote {written} analysis files to {ANALYSIS_DIR}")


if __name__ == "__main__":
    main()
