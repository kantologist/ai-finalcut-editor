"""Build a single searchable index of candidate shots for the editing agent.

The editor should reason over these ranked candidates — not raw media files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from .paths import ROOT

METADATA_PATH = ROOT / "workspace" / "metadata" / "media.json"
ANALYSIS_DIR = ROOT / "workspace" / "analysis"
CANDIDATES_PATH = ANALYSIS_DIR / "candidates.json"


class MediaRecord(BaseModel):
    id: str
    path: str
    type: str
    width: int | None = None
    height: int | None = None


class CandidateShot(BaseModel):
    asset: str
    start: float | None = None
    end: float | None = None
    description: str
    score: float = Field(ge=0.0, le=1.0)
    # Kept for filtering / search; omitted from the compact public view if desired.
    media_id: str | None = None
    subjects: list[str] = Field(default_factory=list)
    recommended: bool | None = None
    type: str | None = None
    width: int | None = None
    height: int | None = None
    aspect_ratio: float | None = None
    aspect: str | None = None


def aspect_ratio_info(width: int | None, height: int | None) -> tuple[float | None, str | None]:
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


def load_media(metadata_path: Path = METADATA_PATH) -> dict[str, MediaRecord]:
    if not metadata_path.is_file():
        raise FileNotFoundError(
            f"Missing media inventory: {metadata_path}. Run inspect_media.py first."
        )
    records = [MediaRecord.model_validate(row) for row in json.loads(metadata_path.read_text())]
    return {record.id: record for record in records}


def asset_name(media: MediaRecord) -> str:
    return Path(media.path).name


def analysis_files(analysis_dir: Path = ANALYSIS_DIR) -> list[Path]:
    return sorted(
        path
        for path in analysis_dir.glob("*.json")
        if path.name != CANDIDATES_PATH.name
    )


def candidates_from_analysis(
    path: Path,
    media_by_id: dict[str, MediaRecord],
) -> list[CandidateShot]:
    data = json.loads(path.read_text(encoding="utf-8"))
    media_id = str(data.get("id") or path.stem)
    media = media_by_id.get(media_id)
    if media is None:
        # Fall back to source_path basename when inventory is missing an entry.
        source = data.get("source_path") or f"{media_id}"
        asset = Path(str(source)).name
        media_type = "image" if Path(asset).suffix.lower() in {".heic", ".heif", ".jpg", ".jpeg", ".png"} else "video"
    else:
        asset = asset_name(media)
        media_type = media.type

    shots: list[CandidateShot] = []
    for row in data.get("segments") or []:
        score = row.get("segment_score")
        if score is None:
            score = row.get("score", 0.0)
        start = row.get("start")
        end = row.get("end")
        # Still images: drop trivial 0-duration windows from the compact index.
        if media_type == "image":
            start = None
            end = None
        width = media.width if media is not None else None
        height = media.height if media is not None else None
        ratio, label = aspect_ratio_info(width, height)
        shots.append(
            CandidateShot(
                asset=asset,
                start=start,
                end=end,
                description=str(row.get("description") or "").strip(),
                score=float(score),
                media_id=media_id,
                subjects=list(row.get("subjects") or []),
                recommended=row.get("recommended"),
                type=media_type,
                width=width,
                height=height,
                aspect_ratio=ratio,
                aspect=label,
            )
        )
    return shots


def build_candidates(
    *,
    media_by_id: dict[str, MediaRecord] | None = None,
    min_score: float = 0.0,
    recommended_only: bool = False,
) -> list[CandidateShot]:
    media_by_id = media_by_id or load_media()
    shots: list[CandidateShot] = []
    for path in analysis_files():
        shots.extend(candidates_from_analysis(path, media_by_id))

    if recommended_only:
        shots = [s for s in shots if s.recommended]
    if min_score > 0:
        shots = [s for s in shots if s.score >= min_score]

    shots.sort(key=lambda s: (-s.score, s.asset, s.start if s.start is not None else -1))
    return shots


def compact_dump(shots: list[CandidateShot]) -> list[dict]:
    """Public index shape for the editing agent (includes aspect for framing decisions)."""
    payload: list[dict] = []
    for shot in shots:
        ordered: dict = {"asset": shot.asset}
        if shot.start is not None:
            ordered["start"] = shot.start
        if shot.end is not None:
            ordered["end"] = shot.end
        ordered["description"] = shot.description
        ordered["score"] = shot.score
        if shot.aspect is not None:
            ordered["aspect"] = shot.aspect
        if shot.aspect_ratio is not None:
            ordered["aspect_ratio"] = shot.aspect_ratio
        if shot.width is not None and shot.height is not None:
            ordered["resolution"] = f"{shot.width}x{shot.height}"
        payload.append(ordered)
    return payload


def search_candidates(shots: list[CandidateShot], query: str) -> list[CandidateShot]:
    tokens = [t for t in re.split(r"\s+", query.strip().lower()) if t]
    if not tokens:
        return shots

    def matches(shot: CandidateShot) -> bool:
        haystack = " ".join(
            [
                shot.asset.lower(),
                shot.description.lower(),
                " ".join(s.lower() for s in shot.subjects),
            ]
        )
        return all(token in haystack for token in tokens)

    return [shot for shot in shots if matches(shot)]


def write_candidates(shots: list[CandidateShot], path: Path = CANDIDATES_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(compact_dump(shots), indent=2) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a searchable candidate-shot index from analysis JSON."
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.0,
        help="Only include candidates at or above this segment_score.",
    )
    parser.add_argument(
        "--recommended-only",
        action="store_true",
        help="Only include segments marked recommended by vision analysis.",
    )
    parser.add_argument(
        "--query",
        type=str,
        default=None,
        help="Search the index by keywords (asset, description, subjects).",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        help="Print only the top N matches (after sorting by score).",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Skip writing candidates.json (preview/search only).",
    )
    args = parser.parse_args()

    if not analysis_files():
        print(
            f"No analysis files in {ANALYSIS_DIR}. Run analyze_media.py first.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        shots = build_candidates(
            min_score=args.min_score,
            recommended_only=args.recommended_only,
        )
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    if not args.no_write:
        path = write_candidates(shots)
        print(f"Wrote {len(shots)} candidates → {path}")

    preview = search_candidates(shots, args.query) if args.query else shots
    if args.top is not None:
        preview = preview[: args.top]

    label = f" matching {args.query!r}" if args.query else ""
    print(f"{len(preview)} candidate shots{label}")
    for shot in preview[:20]:
        timing = ""
        if shot.start is not None and shot.end is not None:
            timing = f" [{shot.start:.1f}-{shot.end:.1f}s]"
        print(f"  {shot.score:.2f}  {shot.asset}{timing}  {shot.description}")
    if len(preview) > 20:
        print(f"  … {len(preview) - 20} more")


if __name__ == "__main__":
    main()
