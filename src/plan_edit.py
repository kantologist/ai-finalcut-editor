"""Plan an Edit Decision List (EDL) from the candidate-shot index.

Architecture:
  LLM → validated EDL JSON (Pydantic) → later deterministic FCPXML export

The model must never emit FCPXML directly.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from openai import APIStatusError, OpenAI, RateLimitError
from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator, model_validator

from .paths import ROOT

METADATA_PATH = ROOT / "workspace" / "metadata" / "media.json"
CANDIDATES_PATH = ROOT / "workspace" / "analysis" / "candidates.json"
EDITS_DIR = ROOT / "workspace" / "edits"
PROMPT_PATH = ROOT / "prompts" / "editor.md"
REVISE_PROMPT_PATH = ROOT / "prompts" / "revise.md"

DEFAULT_MODEL = "gpt-5.4"
DEFAULT_TITLE = "Cinematic Travel Film"
DEFAULT_TARGET_DURATION = 90.0
DEFAULT_MAX_RETRIES = 12
DEFAULT_STILL_DURATION = 2.5
MAX_VIDEO_SOURCE_DURATION = 6.0
IMAGE_EXTENSIONS = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".gif", ".bmp"}


def active_plan_limits() -> tuple[float, float, str, int]:
    """Return (max_video_duration, still_duration, model, max_retries)."""
    try:
        from .settings import load_settings

        cfg = load_settings()
        return (
            float(cfg.get("max_video_source_duration", MAX_VIDEO_SOURCE_DURATION)),
            float(cfg.get("default_still_duration", DEFAULT_STILL_DURATION)),
            str(cfg.get("model", DEFAULT_MODEL)),
            int(cfg.get("max_retries", DEFAULT_MAX_RETRIES)),
        )
    except Exception:  # noqa: BLE001
        return MAX_VIDEO_SOURCE_DURATION, DEFAULT_STILL_DURATION, DEFAULT_MODEL, DEFAULT_MAX_RETRIES

RETRY_HINT_RE = re.compile(
    r"try again in\s+(?P<amount>[\d.]+)\s*(?P<unit>ms|s|m|minutes?)?",
    re.IGNORECASE,
)


class MediaRecord(BaseModel):
    id: str
    path: str
    type: str
    duration: float | None = None
    width: int | None = None
    height: int | None = None


class CandidateShot(BaseModel):
    asset: str
    start: float | None = None
    end: float | None = None
    description: str
    score: float
    width: int | None = None
    height: int | None = None
    aspect_ratio: float | None = None
    aspect: str | None = None


def aspect_ratio_info(width: int | None, height: int | None) -> tuple[float | None, str | None]:
    """Return (numeric ratio w/h, human label) for editorial prompting."""
    if width is None or height is None or width <= 0 or height <= 0:
        return None, None
    ratio = round(width / height, 4)
    if ratio >= 2.3:
        label = "ultrawide"
    elif ratio >= 1.7:
        label = "16:9"
    elif ratio >= 1.5:
        label = "3:2"
    elif ratio >= 1.25:
        label = "4:3"
    elif ratio >= 0.9:
        label = "1:1"
    elif ratio >= 0.7:
        label = "3:4"
    else:
        label = "9:16"
    return ratio, label


class TimelineClip(BaseModel):
    asset: str = Field(description="Filename from the candidate index, e.g. IMG_4821.MOV")
    source_start: float | None = Field(
        default=None,
        description="In-point on the source media in seconds. Omit for still images.",
        ge=0,
    )
    source_duration: float = Field(
        description="How long this clip/still plays on the timeline, in seconds.",
        gt=0,
    )
    reason: str = Field(description="Short editorial justification for this cut.")

    @field_validator("asset")
    @classmethod
    def asset_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("asset must be a non-empty filename")
        return value

    @model_validator(mode="after")
    def stills_omit_source_start(self) -> TimelineClip:
        if Path(self.asset).suffix.lower() in IMAGE_EXTENSIONS and self.source_start is not None:
            # Normalize: stills are holds, not in/out trims.
            self.source_start = None
        return self


class EditDecisionList(BaseModel):
    title: str
    target_duration: float = Field(gt=0)
    timeline: list[TimelineClip] = Field(min_length=1)

    @property
    def actual_duration(self) -> float:
        return round(sum(clip.source_duration for clip in self.timeline), 2)


class ValidatedEdit(BaseModel):
    """EDL plus deterministic checks against inventory/candidates."""

    edl: EditDecisionList
    actual_duration: float
    warnings: list[str] = Field(default_factory=list)


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def load_revise_prompt() -> str:
    return REVISE_PROMPT_PATH.read_text(encoding="utf-8").strip()


def load_edl_file(path: Path) -> EditDecisionList:
    if not path.is_file():
        raise FileNotFoundError(f"Missing EDL: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    # Ignore helper fields persisted alongside the schema.
    payload = {
        "title": data["title"],
        "target_duration": data["target_duration"],
        "timeline": data["timeline"],
    }
    return EditDecisionList.model_validate(payload)


def load_media(metadata_path: Path = METADATA_PATH) -> dict[str, MediaRecord]:
    if not metadata_path.is_file():
        raise FileNotFoundError(
            f"Missing media inventory: {metadata_path}. Run inspect_media.py first."
        )
    records = [MediaRecord.model_validate(row) for row in json.loads(metadata_path.read_text())]
    by_asset = {Path(record.path).name: record for record in records}
    return by_asset


def load_candidates(path: Path = CANDIDATES_PATH) -> list[CandidateShot]:
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing candidates index: {path}. Run rank_segments.py first."
        )
    rows = json.loads(path.read_text())
    media_by_asset = load_media() if METADATA_PATH.is_file() else {}
    shots: list[CandidateShot] = []
    for row in rows:
        asset = str(row.get("asset") or "")
        media = media_by_asset.get(asset)
        width = row.get("width")
        height = row.get("height")
        if width is None and media is not None:
            width = media.width
        if height is None and media is not None:
            height = media.height
        ratio = row.get("aspect_ratio")
        label = row.get("aspect")
        if ratio is None or label is None:
            computed_ratio, computed_label = aspect_ratio_info(
                int(width) if width is not None else None,
                int(height) if height is not None else None,
            )
            if ratio is None:
                ratio = computed_ratio
            if label is None:
                label = computed_label
        shots.append(
            CandidateShot(
                asset=asset,
                start=row.get("start"),
                end=row.get("end"),
                description=str(row.get("description") or ""),
                score=float(row.get("score") or 0.0),
                width=int(width) if width is not None else None,
                height=int(height) if height is not None else None,
                aspect_ratio=float(ratio) if ratio is not None else None,
                aspect=str(label) if label is not None else None,
            )
        )
    return shots


def retry_after_seconds(exc: Exception, attempt: int) -> float:
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
    match = RETRY_HINT_RE.search(str(exc))
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
            print(f"  rate limited on {label}; sleeping {wait:.1f}s", flush=True)
            time.sleep(wait)
        except APIStatusError as exc:
            if exc.status_code != 429:
                raise
            last_exc = exc
            if attempt >= max_retries:
                break
            wait = retry_after_seconds(exc, attempt)
            print(f"  rate limited on {label}; sleeping {wait:.1f}s", flush=True)
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def candidates_brief(candidates: list[CandidateShot], *, limit: int | None = None) -> str:
    rows = candidates if limit is None else candidates[:limit]
    payload = []
    for shot in rows:
        item = {
            "asset": shot.asset,
            "description": shot.description,
            "score": shot.score,
        }
        if shot.start is not None:
            item["start"] = shot.start
        if shot.end is not None:
            item["end"] = shot.end
        if shot.aspect is not None:
            item["aspect"] = shot.aspect
        if shot.aspect_ratio is not None:
            item["aspect_ratio"] = shot.aspect_ratio
        if shot.width is not None and shot.height is not None:
            item["resolution"] = f"{shot.width}x{shot.height}"
        payload.append(item)
    return json.dumps(payload, indent=2)


def _allowed_assets(candidates: list[CandidateShot], *, limit: int | None = None) -> tuple[str, ...]:
    rows = candidates if limit is None else candidates[:limit]
    assets = tuple(sorted({shot.asset for shot in rows}))
    if not assets:
        raise ValueError("Candidate index has no assets to plan from.")
    return assets


def constrained_edl_model(allowed_assets: tuple[str, ...]) -> type[BaseModel]:
    """Build an EDL schema whose `asset` field is an enum of candidate filenames.

    Structured outputs then reject invented DJI-style names before validation.
    """
    asset_literal: Any = Literal.__getitem__(allowed_assets)
    clip_model = create_model(
        "ConstrainedTimelineClip",
        __config__=ConfigDict(extra="forbid"),
        asset=(asset_literal, Field(description="Exact filename from the candidate index")),
        source_start=(
            float | None,
            Field(
                default=None,
                description="In-point on the source media in seconds. Omit for still images.",
                ge=0,
            ),
        ),
        source_duration=(
            float,
            Field(
                description="How long this clip/still plays on the timeline, in seconds.",
                gt=0,
            ),
        ),
        reason=(str, Field(description="Short editorial justification for this cut.")),
    )
    return create_model(
        "ConstrainedEditDecisionList",
        __config__=ConfigDict(extra="forbid"),
        title=(str, ...),
        target_duration=(float, Field(gt=0)),
        timeline=(list[clip_model], Field(min_length=1)),  # type: ignore[valid-type]
    )


def _parse_edl(
    client: OpenAI,
    *,
    model: str,
    system_prompt: str,
    user_text: str,
    candidates: list[CandidateShot],
    candidate_limit: int | None,
    label: str,
    max_retries: int,
) -> EditDecisionList:
    allowed = _allowed_assets(candidates, limit=candidate_limit)
    response_format = constrained_edl_model(allowed)

    def _call() -> EditDecisionList:
        completion = client.chat.completions.parse(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            response_format=response_format,
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError(f"Model returned no parsed EDL ({label})")
        return EditDecisionList.model_validate(parsed.model_dump())

    return with_rate_limit_retries(_call, max_retries=max_retries, label=label)


def delivery_frame_note() -> str:
    """Remind the model of the live sequence size from settings."""
    try:
        from .settings import load_settings

        cfg = load_settings()
        width = int(cfg.get("sequence_width", 1080))
        height = int(cfg.get("sequence_height", 1920))
        max_hold = float(cfg.get("max_video_source_duration", MAX_VIDEO_SOURCE_DURATION))
    except Exception:  # noqa: BLE001
        width, height = 1080, 1920
        max_hold = MAX_VIDEO_SOURCE_DURATION
    _, label = aspect_ratio_info(width, height)
    label = label or f"{width}x{height}"
    return (
        f"Delivery sequence is {width}×{height} ({label}). "
        "Prefer candidates whose aspect matches; wide/landscape sources Fit (letterbox), "
        "vertical sources Fill. "
        "Group vertical/square shots first, then landscape/wide. "
        f"Prefer ~4–6s holds per shot (max {max_hold:.1f}s)."
    )


def _clip_is_vertical_or_square(
    clip: TimelineClip,
    *,
    media_by_asset: dict[str, MediaRecord],
    aspect_by_asset: dict[str, str],
) -> bool:
    media = media_by_asset.get(clip.asset)
    if media is not None and media.width and media.height:
        ratio, _ = aspect_ratio_info(media.width, media.height)
        if ratio is not None:
            return ratio <= 1.05
    label = aspect_by_asset.get(clip.asset)
    if label:
        return label in {"9:16", "3:4", "1:1", "4:5"}
    # Unknown geometry: keep with the vertical block so it is not demoted.
    return True


def group_vertical_before_landscape(
    timeline: list[TimelineClip],
    *,
    media_by_asset: dict[str, MediaRecord],
    candidates: list[CandidateShot],
) -> tuple[list[TimelineClip], bool]:
    """Stable-partition: vertical/square body, then landscape, closer stays last."""
    if len(timeline) <= 1:
        return timeline, False

    aspect_by_asset = {
        shot.asset: shot.aspect for shot in candidates if shot.aspect
    }
    closer = timeline[-1]
    body = timeline[:-1]
    vertical: list[TimelineClip] = []
    landscape: list[TimelineClip] = []
    for clip in body:
        if _clip_is_vertical_or_square(
            clip, media_by_asset=media_by_asset, aspect_by_asset=aspect_by_asset
        ):
            vertical.append(clip)
        else:
            landscape.append(clip)
    regrouped = [*vertical, *landscape, closer]
    changed = [c.asset for c in regrouped] != [c.asset for c in timeline]
    return regrouped, changed


def plan_edl(
    client: OpenAI,
    *,
    candidates: list[CandidateShot],
    title: str,
    target_duration: float,
    brief: str | None,
    model: str,
    system_prompt: str,
    max_retries: int,
    candidate_limit: int | None,
) -> EditDecisionList:
    # Creative brief lives in prompts/editor.md. User message carries the shot index.
    user_text = (
        f"Working title: {title}\n"
        f"Target duration: {target_duration} seconds\n"
        f"{delivery_frame_note()}\n"
        "Follow the creative brief in the system prompt.\n"
        "Hold shots a bit longer before cutting (~4–6s when the window allows).\n"
        "Group vertical/square coverage first, then landscape/wide; keep the closer last.\n"
        "Copy every `asset` filename EXACTLY from the candidate index. Never invent names.\n"
    )
    if brief:
        user_text += f"\nAdditional notes:\n{brief}\n"
    user_text += (
        "\nCandidate-shot index (use only these assets):\n"
        f"{candidates_brief(candidates, limit=candidate_limit)}\n"
        "\nReturn the EDL JSON now."
    )
    return _parse_edl(
        client,
        model=model,
        system_prompt=system_prompt,
        user_text=user_text,
        candidates=candidates,
        candidate_limit=candidate_limit,
        label="plan_edit",
        max_retries=max_retries,
    )


def revise_edl(
    client: OpenAI,
    *,
    current: EditDecisionList,
    candidates: list[CandidateShot],
    notes: str,
    model: str,
    system_prompt: str,
    max_retries: int,
    candidate_limit: int | None = None,
) -> EditDecisionList:
    """Ask the model for a full replacement EDL given revision notes."""
    user_text = (
        "Revise the following Edit Decision List according to the notes.\n\n"
        f"Revision notes:\n{notes.strip()}\n\n"
        f"{delivery_frame_note()}\n\n"
        f"Current EDL:\n{json.dumps(current.model_dump(exclude_none=True), indent=2)}\n\n"
        "Candidate-shot index (use only these assets):\n"
        f"{candidates_brief(candidates, limit=candidate_limit)}\n\n"
        "Copy every `asset` filename EXACTLY from the candidate index. Never invent names.\n"
        "Return the full revised EDL JSON now."
    )
    return _parse_edl(
        client,
        model=model,
        system_prompt=system_prompt,
        user_text=user_text,
        candidates=candidates,
        candidate_limit=candidate_limit,
        label="revise_edit",
        max_retries=max_retries,
    )


def _candidate_windows(candidates: list[CandidateShot]) -> dict[str, list[tuple[float | None, float | None]]]:
    windows: dict[str, list[tuple[float | None, float | None]]] = {}
    for shot in candidates:
        windows.setdefault(shot.asset, []).append((shot.start, shot.end))
    return windows


def validate_against_media(
    edl: EditDecisionList,
    *,
    media_by_asset: dict[str, MediaRecord],
    candidates: list[CandidateShot],
    clamp: bool = True,
) -> ValidatedEdit:
    """Deterministic guardrails after Pydantic parse."""
    warnings: list[str] = []
    known_assets = {shot.asset for shot in candidates}
    windows = _candidate_windows(candidates)
    fixed: list[TimelineClip] = []
    max_video_duration, still_duration, _, _ = active_plan_limits()

    for index, clip in enumerate(edl.timeline):
        if clip.asset not in known_assets:
            if clamp:
                warnings.append(
                    f"timeline[{index}] dropped unknown asset not in candidates: {clip.asset}"
                )
                continue
            raise ValueError(f"timeline[{index}] unknown asset not in candidates: {clip.asset}")
        media = media_by_asset.get(clip.asset)
        if media is None:
            if clamp:
                warnings.append(
                    f"timeline[{index}] dropped asset missing from media inventory: {clip.asset}"
                )
                continue
            raise ValueError(f"timeline[{index}] asset missing from media inventory: {clip.asset}")

        is_image = media.type == "image" or Path(clip.asset).suffix.lower() in IMAGE_EXTENSIONS
        source_start = clip.source_start
        source_duration = clip.source_duration

        if is_image:
            if source_start is not None:
                warnings.append(f"{clip.asset}: cleared source_start on still")
                source_start = None
            if source_duration <= 0:
                source_duration = still_duration
                warnings.append(f"{clip.asset}: defaulted still duration to {still_duration}s")
        else:
            if source_start is None:
                # Prefer the first candidate window start for this asset.
                starts = [w[0] for w in windows.get(clip.asset, []) if w[0] is not None]
                source_start = starts[0] if starts else 0.0
                warnings.append(f"{clip.asset}: filled missing source_start → {source_start}")

            if source_duration > max_video_duration + 1e-6:
                if clamp:
                    warnings.append(
                        f"{clip.asset}: clamped video duration "
                        f"{source_duration:.2f} → {max_video_duration:.1f}s (brief max)"
                    )
                    source_duration = max_video_duration
                else:
                    raise ValueError(
                        f"timeline[{index}] video source_duration "
                        f"{source_duration} exceeds {max_video_duration}s brief limit"
                    )

            asset_duration = media.duration
            if asset_duration is not None and source_start > asset_duration:
                raise ValueError(
                    f"timeline[{index}] source_start {source_start} beyond duration {asset_duration}"
                )

            # Prefer staying inside a known candidate window when possible.
            matched = None
            for start, end in windows.get(clip.asset, []):
                if start is None or end is None:
                    continue
                if start - 1e-6 <= source_start <= end + 1e-6:
                    matched = (start, end)
                    break
            if matched is None:
                # If start missed every window, snap into the highest-scoring overlap by proximity.
                best = None
                best_dist = None
                for start, end in windows.get(clip.asset, []):
                    if start is None or end is None:
                        continue
                    if source_start < start:
                        dist = start - source_start
                    elif source_start > end:
                        dist = source_start - end
                    else:
                        dist = 0.0
                    if best_dist is None or dist < best_dist:
                        best = (start, end)
                        best_dist = dist
                matched = best

            if matched is not None:
                window_start, window_end = matched
                window_len = max(0.1, window_end - window_start)
                desired = min(source_duration, max_video_duration, window_len)
                # If the chosen in-point is too close to the window end, pull it earlier.
                if window_end - source_start < desired - 1e-6:
                    new_start = round(max(window_start, window_end - desired), 2)
                    if new_start != source_start:
                        warnings.append(
                            f"{clip.asset}: adjusted source_start {source_start} → {new_start} "
                            f"to fit {desired:.2f}s inside candidate window"
                        )
                        source_start = new_start
                max_dur = max(0.1, window_end - source_start)
                if source_duration > max_dur + 1e-6:
                    if clamp:
                        warnings.append(
                            f"{clip.asset}: clamped source_duration {source_duration:.2f} → {max_dur:.2f}"
                        )
                        source_duration = round(max_dur, 2)
                    else:
                        raise ValueError(
                            f"timeline[{index}] source_duration extends past candidate end {window_end}"
                        )
            elif asset_duration is not None:
                max_dur = max(0.1, asset_duration - source_start)
                if source_duration > max_dur + 1e-6:
                    if clamp:
                        warnings.append(
                            f"{clip.asset}: clamped source_duration to asset end ({max_dur:.2f}s)"
                        )
                        source_duration = round(max_dur, 2)
                    else:
                        raise ValueError(
                            f"timeline[{index}] source_duration exceeds asset duration"
                        )

        if fixed and fixed[-1].asset == clip.asset and not is_image:
            msg = f"timeline[{index}] adjacent to previous clip from same source video ({clip.asset})"
            if clamp:
                warnings.append(msg)
            else:
                raise ValueError(msg)

        fixed.append(
            TimelineClip(
                asset=clip.asset,
                source_start=source_start,
                source_duration=round(source_duration, 2),
                reason=clip.reason,
            )
        )

    if not fixed:
        raise ValueError("EDL timeline empty after removing unknown / invalid assets")

    regrouped, reordered = group_vertical_before_landscape(
        fixed,
        media_by_asset=media_by_asset,
        candidates=candidates,
    )
    if reordered:
        adjacent_ok = True
        for index in range(1, len(regrouped)):
            prev = regrouped[index - 1]
            cur = regrouped[index]
            prev_image = Path(prev.asset).suffix.lower() in IMAGE_EXTENSIONS
            cur_image = Path(cur.asset).suffix.lower() in IMAGE_EXTENSIONS
            if not prev_image and not cur_image and prev.asset == cur.asset:
                adjacent_ok = False
                break
        if adjacent_ok:
            warnings.append(
                "reordered timeline: vertical/square shots first, then landscape (closer kept last)"
            )
            fixed = regrouped
        else:
            warnings.append(
                "skipped vertical-first reorder — would place adjacent clips from the same source"
            )

    cleaned = EditDecisionList(
        title=edl.title,
        target_duration=edl.target_duration,
        timeline=fixed,
    )
    ratio = cleaned.actual_duration / cleaned.target_duration
    if ratio < 0.7 or ratio > 1.3:
        warnings.append(
            f"actual duration {cleaned.actual_duration}s is far from target {cleaned.target_duration}s"
        )

    still_count = sum(
        1
        for clip in cleaned.timeline
        if Path(clip.asset).suffix.lower() in IMAGE_EXTENSIONS
    )
    if still_count > max(2, len(cleaned.timeline) // 5):
        warnings.append(
            f"still photos used {still_count}/{len(cleaned.timeline)} times — brief says use sparingly"
        )

    return ValidatedEdit(
        edl=cleaned,
        actual_duration=cleaned.actual_duration,
        warnings=warnings,
    )


def write_edl(
    validated: ValidatedEdit,
    path: Path,
    *,
    version: int | None = None,
    revision_notes: list[str] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = validated.edl.model_dump(exclude_none=True)
    payload["actual_duration"] = validated.actual_duration
    if version is not None:
        payload["version"] = version
    if revision_notes:
        payload["revision_notes"] = revision_notes
    if validated.warnings:
        payload["warnings"] = validated.warnings
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan a validated Edit Decision List from candidates.")
    parser.add_argument("--title", default=DEFAULT_TITLE, help="Working title for the edit.")
    parser.add_argument(
        "--target-duration",
        type=float,
        default=DEFAULT_TARGET_DURATION,
        help=f"Target timeline length in seconds (default: {DEFAULT_TARGET_DURATION}).",
    )
    parser.add_argument(
        "--brief",
        type=str,
        default=None,
        help="Optional editorial brief / story intent for the model.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"OpenAI model (default: {DEFAULT_MODEL})")
    parser.add_argument(
        "--candidate-limit",
        type=int,
        default=None,
        help="Max candidates to send (default: full index, highest scores first).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=EDITS_DIR / "edit.json",
        help="Where to write the validated EDL JSON.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Retries on 429 rate limits (default: {DEFAULT_MAX_RETRIES}).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail instead of clamping out-of-range source_duration values.",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("OPENAI_API_KEY is missing. Add it to .env", file=sys.stderr)
        sys.exit(1)

    try:
        candidates = load_candidates()
        media_by_asset = load_media()
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    if not candidates:
        print("Candidate index is empty. Run analyze_media.py + rank_segments.py first.", file=sys.stderr)
        sys.exit(1)

    # Candidates file is already score-sorted.
    client = OpenAI(api_key=api_key, max_retries=0)
    sent = len(candidates) if args.candidate_limit is None else min(len(candidates), args.candidate_limit)
    print(
        f"Planning EDL '{args.title}' (~{args.target_duration}s) "
        f"from {sent}/{len(candidates)} candidates …",
        flush=True,
    )

    try:
        edl = plan_edl(
            client,
            candidates=candidates,
            title=args.title,
            target_duration=args.target_duration,
            brief=args.brief,
            model=args.model,
            system_prompt=load_prompt(),
            max_retries=args.max_retries,
            candidate_limit=args.candidate_limit,
        )
        # Ensure target/title from CLI win if the model drifts.
        edl = edl.model_copy(
            update={
                "title": args.title or edl.title,
                "target_duration": args.target_duration,
            }
        )
        validated = validate_against_media(
            edl,
            media_by_asset=media_by_asset,
            candidates=candidates,
            clamp=not args.strict,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"failed: {exc}", file=sys.stderr)
        sys.exit(1)

    path = write_edl(validated, args.output)
    print(
        f"Wrote EDL ({len(validated.edl.timeline)} clips, "
        f"{validated.actual_duration}s actual / {validated.edl.target_duration}s target) → {path}"
    )
    for warning in validated.warnings:
        print(f"  warning: {warning}")


if __name__ == "__main__":
    main()
