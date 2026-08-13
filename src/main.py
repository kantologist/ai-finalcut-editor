"""End-to-end AI Final Cut editor pipeline.

Example:
  uv run python -m src.main \\
    --input workspace/originals \\
    --duration 90 \\
    --style cinematic \\
    --name "Lagos"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from .analyze_media import (
    ANALYSIS_DIR,
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_RETRIES,
    DEFAULT_REQUEST_PAUSE_SEC,
    MediaRecord as AnalysisMediaRecord,
    analyze_clip,
    analyze_still,
    load_prompt as load_vision_prompt,
    write_analysis,
)
from .generate_fcpxml import build_fcpxml, load_edl, load_media as load_fcpxml_media, write_fcpxml
from .inspect_media import METADATA_DIR, OUTPUT_PATH as MEDIA_JSON, MediaRecord as InspectMediaRecord, inspect_media
from .make_proxies import extract_frames, load_videos
from .plan_edit import (
    DEFAULT_MAX_RETRIES as PLAN_MAX_RETRIES,
    load_candidates,
    load_media as load_plan_media,
    load_prompt as load_editor_prompt,
    plan_edl,
    validate_against_media,
    write_edl,
)
from .paths import ROOT
from .rank_segments import build_candidates, write_candidates

DEFAULT_INPUT = ROOT / "workspace" / "originals"
OUTPUT_DIR = ROOT / "workspace" / "output"
EDITS_DIR = ROOT / "workspace" / "edits"

STYLE_BRIEFS: dict[str, str] = {
    "cinematic": (
        "Style: cinematic vertical travel film (9:16) — taller establishing holds (~4–6s), "
        "vertical/square block first then landscape, rising pace, strong closer."
    ),
    "documentary": (
        "Style: documentary vertical (9:16) — observational coverage, unhurried holds, "
        "vertical coverage first then wide B-roll, favor story_value and center framing."
    ),
    "social": (
        "Style: social vertical highlight reel (9:16) — energetic but not frantic (~3–5s holds), "
        "phone-first vertical block first, landscape accents later."
    ),
    "highlight": (
        "Style: vertical highlight reel (9:16) — strongest moments with slightly longer holds, "
        "portrait/square grouped first, landscape only after, minimal stills."
    ),
}

STRONG_SCORE = 0.65
PIPELINE_STAGES = ("scan", "proxies", "analyze", "candidates", "plan", "fcpxml")
CANDIDATES_PATH = ROOT / "workspace" / "analysis" / "candidates.json"


def active_style_briefs() -> dict[str, str]:
    try:
        from .settings import load_settings

        briefs = load_settings().get("style_briefs") or STYLE_BRIEFS
        return {str(k): str(v) for k, v in briefs.items()}
    except Exception:  # noqa: BLE001
        return dict(STYLE_BRIEFS)


def active_strong_score() -> float:
    try:
        from .settings import load_settings

        return float(load_settings().get("strong_score", STRONG_SCORE))
    except Exception:  # noqa: BLE001
        return STRONG_SCORE


def active_model() -> str:
    try:
        from .settings import load_settings

        return str(load_settings().get("model") or "gpt-5.4")
    except Exception:  # noqa: BLE001
        return "gpt-5.4"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower())
    return slug.strip("_") or "edit"


def _count_frames() -> int:
    frames_dir = ROOT / "workspace" / "frames"
    if not frames_dir.is_dir():
        return 0
    return sum(1 for p in frames_dir.glob("*.jpg") if p.is_file())


def _analysis_files() -> list[Path]:
    if not ANALYSIS_DIR.is_dir():
        return []
    return sorted(p for p in ANALYSIS_DIR.glob("*.json") if p.name != "candidates.json")


def _has_valid_edl(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    timeline = data.get("timeline")
    if timeline is None and isinstance(data.get("edl"), dict):
        timeline = data["edl"].get("timeline")
    return isinstance(timeline, list) and len(timeline) > 0


def detect_create_resume_stage(*, slug: str, force: bool = False) -> str:
    """Return the first incomplete pipeline stage for this project slug."""
    if force:
        return "scan"

    edl_path = EDITS_DIR / f"{slug}_v1.json"
    output_path = OUTPUT_DIR / f"{slug}_v1.fcpxml"
    if _has_valid_edl(edl_path) and output_path.is_file():
        return "done"
    if _has_valid_edl(edl_path):
        return "fcpxml"
    if CANDIDATES_PATH.is_file() and _analysis_files():
        return "plan"
    if _analysis_files():
        return "candidates"
    if MEDIA_JSON.is_file() and _count_frames() > 0:
        return "analyze"
    if MEDIA_JSON.is_file():
        return "proxies"
    return "scan"


def _print(msg: str = "") -> None:
    print(msg, flush=True)


ProgressFn = Callable[[str], None]


def run_pipeline(
    *,
    input_dir: Path,
    duration: float,
    style: str,
    name: str,
    force: bool = False,
    skip_analyze: bool = False,
    resume: bool = False,
    model: str = "gpt-5.4",
    min_score: float = STRONG_SCORE,
    on_progress: ProgressFn | None = None,
) -> Path:
    def emit(msg: str = "") -> None:
        _print(msg)
        if on_progress is not None:
            on_progress(msg)

    load_dotenv(ROOT / ".env", override=True)
    from .secrets import read_openai_api_key

    api_key = read_openai_api_key()
    input_dir = input_dir.resolve()
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")

    style_key = style.lower().strip()
    briefs = active_style_briefs()
    style_brief = briefs.get(style_key) or briefs.get("cinematic") or next(iter(briefs.values()))
    if style_key not in briefs:
        style_brief = f"Style: {style}. " + style_brief

    if min_score == STRONG_SCORE:
        min_score = active_strong_score()
    if model == "gpt-5.4":
        model = active_model()

    try:
        from .settings import load_settings

        cfg = load_settings()
        batch_size = int(cfg.get("vision_batch_size", DEFAULT_BATCH_SIZE))
        max_retries = int(cfg.get("max_retries", DEFAULT_MAX_RETRIES))
        request_pause = float(cfg.get("request_pause_sec", DEFAULT_REQUEST_PAUSE_SEC))
        plan_retries = int(cfg.get("max_retries", PLAN_MAX_RETRIES))
    except Exception:  # noqa: BLE001
        batch_size = DEFAULT_BATCH_SIZE
        max_retries = DEFAULT_MAX_RETRIES
        request_pause = DEFAULT_REQUEST_PAUSE_SEC
        plan_retries = PLAN_MAX_RETRIES

    slug = slugify(name)
    version = 1
    project_name = f"{name.strip()} v{version}"
    edl_path = EDITS_DIR / f"{slug}_v{version}.json"
    output_path = OUTPUT_DIR / f"{slug}_v{version}.fcpxml"

    start_stage = "scan"
    if resume and not force:
        start_stage = detect_create_resume_stage(slug=slug, force=False)
        if start_stage == "done":
            emit(f"Outputs already complete — reusing {output_path.relative_to(ROOT)}")
            return output_path
        emit(f"Resuming from stage: {start_stage}")
        emit()

    stage_index = {name: i for i, name in enumerate(PIPELINE_STAGES)}

    def should_run(stage: str) -> bool:
        return stage_index[stage] >= stage_index[start_stage]

    records: list[InspectMediaRecord] = []

    # ------------------------------------------------------------------ scan
    if should_run("scan"):
        files_on_disk = [
            p
            for p in sorted(input_dir.rglob("*"))
            if p.is_file() and not p.name.startswith(".")
        ]
        emit(f"Scanning {len(files_on_disk)} assets...")
        records = inspect_media(input_dir)
        METADATA_DIR.mkdir(parents=True, exist_ok=True)
        MEDIA_JSON.write_text(
            json.dumps([r.model_dump() for r in records], indent=2) + "\n",
            encoding="utf-8",
        )
        videos = [r for r in records if r.type == "video"]
        photos = [r for r in records if r.type == "image"]
        emit(f"Found {len(videos)} videos / {len(photos)} photos")
        emit()

        emit("Analyzing video metadata...")
        emit(f"  Indexed {len(records)} assets → {MEDIA_JSON.relative_to(ROOT)}")
        emit()
    else:
        emit(f"Skipping scan — reusing {MEDIA_JSON.relative_to(ROOT)}")
        raw = json.loads(MEDIA_JSON.read_text(encoding="utf-8"))
        records = [InspectMediaRecord.model_validate(row) for row in raw]
        emit()

    # --------------------------------------------------------------- proxies
    if should_run("proxies"):
        proxy_videos = load_videos(MEDIA_JSON)
        emit("Generating proxy frames...")
        for index, record in enumerate(proxy_videos, start=1):
            frames = extract_frames(record, force=force)
            emit(f"  [{index}/{len(proxy_videos)}] {record.id} → {len(frames)} frames")
        total_frames = _count_frames()
        emit(f"Generating {total_frames:,} proxy frames... done")
        emit()
    else:
        emit(f"Skipping proxies — {_count_frames():,} frames already on disk")
        emit()

    # --------------------------------------------------------------- analyze
    if should_run("analyze"):
        emit("Analyzing scenes...")
        if skip_analyze:
            existing = _analysis_files()
            if not existing:
                raise RuntimeError("No analysis files found. Re-run without --skip-analyze.")
            emit(f"  skipped (--skip-analyze); reusing {len(existing)} files")
        else:
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY is missing. Add it to .env")
            client = OpenAI(api_key=api_key, max_retries=0)
            vision_prompt = load_vision_prompt()
            targets = [
                AnalysisMediaRecord(
                    id=r.id,
                    path=r.path,
                    type=r.type,
                    duration=r.duration,
                    favorite=r.favorite,
                )
                for r in records
            ]
            written = 0
            for index, media in enumerate(targets, start=1):
                out = ANALYSIS_DIR / f"{media.id}.json"
                if out.exists() and not force:
                    emit(f"  [{index}/{len(targets)}] {media.id} — skip (exists)")
                    continue
                emit(f"  [{index}/{len(targets)}] {media.id}...")
                if media.type == "image":
                    analysis = analyze_still(
                        client,
                        media,
                        model=model,
                        system_prompt=vision_prompt,
                        max_retries=max_retries,
                    )
                else:
                    analysis = analyze_clip(
                        client,
                        media,
                        model=model,
                        batch_size=batch_size,
                        system_prompt=vision_prompt,
                        max_retries=max_retries,
                        request_pause_sec=request_pause,
                    )
                write_analysis(analysis)
                written += 1
            emit(f"  Wrote {written} analysis files")
        emit()
    elif start_stage != "fcpxml":
        existing = _analysis_files()
        emit(f"Skipping analyze — reusing {len(existing)} files")
        emit()

    # ------------------------------------------------------------- candidates
    candidates = []
    if should_run("candidates"):
        emit("Building candidate index...")
        candidates = build_candidates()
        write_candidates(candidates)
        strong = [c for c in candidates if c.score >= min_score]
        emit()
        emit(f"Found {len(strong)} strong candidate segments")
        emit()
    elif should_run("plan"):
        emit(f"Skipping candidates — reusing {CANDIDATES_PATH.relative_to(ROOT)}")
        candidates = load_candidates()
        emit()

    # ------------------------------------------------------------------- plan
    if should_run("plan"):
        emit(f"Planning {int(duration)}-second edit...")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is missing. Add it to .env")
        client = OpenAI(api_key=api_key, max_retries=0)
        title = f"{name.strip()} — {style_key.capitalize()}"
        edl = plan_edl(
            client,
            candidates=candidates or load_candidates(),
            title=title,
            target_duration=float(duration),
            brief=style_brief,
            model=model,
            system_prompt=load_editor_prompt(),
            max_retries=plan_retries,
            candidate_limit=None,
        )
        edl = edl.model_copy(update={"title": title, "target_duration": float(duration)})
        validated = validate_against_media(
            edl,
            media_by_asset=load_plan_media(),
            candidates=load_candidates(),
            clamp=True,
        )
        EDITS_DIR.mkdir(parents=True, exist_ok=True)
        write_edl(validated, edl_path, version=1)
        selected_assets = sorted({clip.asset for clip in validated.edl.timeline})
        emit(f"Selected {len(selected_assets)} assets")
        emit(f"  {len(validated.edl.timeline)} cuts · {validated.actual_duration:.1f}s actual")
        for warning in validated.warnings[:5]:
            emit(f"  warning: {warning}")
        emit()
    else:
        emit(f"Skipping plan — reusing {edl_path.relative_to(ROOT)}")
        emit()

    # ----------------------------------------------------------------- fcpxml
    emit("Generating FCPXML...")
    clips = load_edl(edl_path)
    media = load_fcpxml_media()
    root = build_fcpxml(
        clips,
        media,
        event_name="AI Travel Editor",
        project_name=project_name,
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    write_fcpxml(root, output_path)
    emit()
    emit(f"✓ {output_path.relative_to(ROOT)}")
    return output_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Run the full AI Final Cut editor pipeline.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="Folder of originals (default: workspace/originals)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=90,
        help="Target edit duration in seconds (default: 90)",
    )
    parser.add_argument(
        "--style",
        default="cinematic",
        choices=sorted(STYLE_BRIEFS),
        help="Editorial style preset (default: cinematic)",
    )
    parser.add_argument(
        "--name",
        default="Lagos",
        help='Project name used in titles/output (default: "Lagos")',
    )
    parser.add_argument(
        "--model",
        default="gpt-5.4",
        help="OpenAI model for vision + edit planning",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate proxies/analysis even when outputs exist",
    )
    parser.add_argument(
        "--skip-analyze",
        action="store_true",
        help="Skip vision analysis and reuse existing analysis JSON",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from the last completed pipeline stage using on-disk artifacts",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=STRONG_SCORE,
        help=f"Threshold for 'strong' candidate count (default: {STRONG_SCORE})",
    )
    args = parser.parse_args(argv)

    try:
        run_pipeline(
            input_dir=args.input,
            duration=args.duration,
            style=args.style,
            name=args.name,
            force=args.force,
            skip_analyze=args.skip_analyze,
            resume=args.resume,
            model=args.model,
            min_score=args.min_score,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
