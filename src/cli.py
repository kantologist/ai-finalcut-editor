"""Conversational AI Final Cut editor CLI.

Examples:
  ai-edit create --album-export workspace/originals --duration 90
  ai-edit revise workspace/edits/lagos_v1.json "Add more people and faster cuts."
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

from .generate_fcpxml import build_fcpxml, load_edl, load_media as load_fcpxml_media, write_fcpxml
from .main import ROOT, active_style_briefs, run_pipeline, slugify
from .plan_edit import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MODEL,
    EditDecisionList,
    TimelineClip,
    load_candidates,
    load_edl_file,
    load_media,
    load_revise_prompt,
    revise_edl,
    validate_against_media,
    write_edl,
)
from .settings import load_settings

EDITS_DIR = ROOT / "workspace" / "edits"
OUTPUT_DIR = ROOT / "workspace" / "output"
VERSION_RE = re.compile(r"^(?P<base>.+)_v(?P<ver>\d+)$", re.IGNORECASE)


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _print(msg: str = "") -> None:
    print(msg, flush=True)


def parse_versioned_stem(stem: str) -> tuple[str, int]:
    match = VERSION_RE.match(stem)
    if match:
        return match.group("base"), int(match.group("ver"))
    return stem, 1


def next_revision_paths(edl_path: Path) -> tuple[str, int, Path, Path]:
    base, current = parse_versioned_stem(edl_path.stem)
    version = current + 1
    edl_out = EDITS_DIR / f"{base}_v{version}.json"
    fcpxml_out = OUTPUT_DIR / f"{base}_v{version}.fcpxml"
    return base, version, edl_out, fcpxml_out


def display_name_from_base(base: str) -> str:
    return base.replace("_", " ").strip().title() or "Edit"


BAN_RE = re.compile(
    r"(?:never use|don't use|do not use|exclude|ban|avoid)\s+([A-Za-z0-9._-]+\.(?:MOV|MP4|HEIC|JPG|JPEG|PNG|mov|mp4|heic|jpg|jpeg|png))",
    re.IGNORECASE,
)
END_WITH_RE = re.compile(
    r"(?:end with|end on|close with|closing(?: shot)?(?: is| with)?)\s+([A-Za-z0-9._-]+\.(?:MOV|MP4|HEIC|JPG|JPEG|PNG|mov|mp4|heic|jpg|jpeg|png))",
    re.IGNORECASE,
)


def parse_hard_constraints(*notes: str) -> tuple[set[str], str | None]:
    banned: set[str] = set()
    end_with: str | None = None
    for note in notes:
        for match in BAN_RE.finditer(note):
            banned.add(match.group(1))
        end_match = END_WITH_RE.search(note)
        if end_match:
            end_with = end_match.group(1)
    return banned, end_with


def apply_hard_constraints(
    edl: EditDecisionList,
    *,
    banned: set[str],
    end_with: str | None,
    candidates_assets: set[str],
) -> EditDecisionList:
    """Deterministically enforce never-use / end-with notes the model may miss."""
    timeline = [clip for clip in edl.timeline if clip.asset not in banned]
    if not timeline:
        raise ValueError("Revision removed every clip; broaden the notes or candidate set.")

    if end_with:
        if end_with not in candidates_assets:
            raise ValueError(f"Requested ending asset not in candidates: {end_with}")
        if end_with in banned:
            raise ValueError(f"Requested ending asset is also banned: {end_with}")

        donor = next((c for c in reversed(edl.timeline) if c.asset == end_with), None)
        if donor is None:
            is_still = end_with.upper().endswith((".HEIC", ".JPG", ".JPEG", ".PNG"))
            donor = TimelineClip(
                asset=end_with,
                source_start=None if is_still else 0.0,
                source_duration=2.5 if is_still else 3.0,
                reason="closing beat (revision)",
            )

        # Remove existing occurrences, then append as the finale.
        timeline = [c for c in timeline if c.asset != end_with]
        if timeline and timeline[-1].asset == donor.asset:
            timeline = timeline[:-1]
        timeline.append(
            TimelineClip(
                asset=donor.asset,
                source_start=donor.source_start,
                source_duration=min(float(donor.source_duration), 4.0),
                reason=donor.reason or "closing beat (revision)",
            )
        )

    return EditDecisionList(
        title=edl.title,
        target_duration=edl.target_duration,
        timeline=timeline,
    )


def cmd_create(args: argparse.Namespace) -> int:
    cfg = load_settings()
    name = args.name or str(cfg.get("default_name") or "Lagos")
    try:
        output = run_pipeline(
            input_dir=Path(args.album_export),
            duration=float(args.duration),
            style=args.style,
            name=name,
            force=args.force,
            skip_analyze=args.skip_analyze,
            model=args.model,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"failed: {exc}", file=sys.stderr)
        return 1

    slug = slugify(name)
    edl = EDITS_DIR / f"{slug}_v1.json"
    _print()
    _print(f"✓ {_rel(edl)}")
    _print(f"✓ {_rel(output)}")
    return 0


def run_revise(
    *,
    edl_path: Path,
    notes: str,
    model: str = DEFAULT_MODEL,
    strict: bool = False,
    resume: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[Path, Path]:
    """Revise an EDL and write the next versioned JSON + FCPXML. Returns (edl, fcpxml)."""

    def emit(msg: str = "") -> None:
        _print(msg)
        if on_progress is not None:
            on_progress(msg)

    edl_path = Path(edl_path).expanduser()
    if not edl_path.is_absolute():
        edl_path = (Path.cwd() / edl_path).resolve()
    notes = notes.strip()
    if not notes:
        raise ValueError("Revision notes are required.")
    if not edl_path.is_file():
        raise FileNotFoundError(f"EDL not found: {edl_path}")

    base, version, edl_out, fcpxml_out = next_revision_paths(edl_path)
    project_name = f"{display_name_from_base(base)} v{version}"

    if resume and edl_out.is_file() and not fcpxml_out.is_file():
        emit(f"Resuming FCPXML for {_rel(edl_out)}")
        clips = load_edl(edl_out)
        root = build_fcpxml(
            clips,
            load_fcpxml_media(),
            event_name="AI Travel Editor",
            project_name=project_name,
        )
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        write_fcpxml(root, fcpxml_out)
        emit(f"✓ {_rel(edl_out)}")
        emit(f"✓ {_rel(fcpxml_out)}")
        return edl_out, fcpxml_out

    load_dotenv(ROOT / ".env", override=True)
    from .secrets import read_openai_api_key

    api_key = read_openai_api_key()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing. Add it in Settings → API key.")

    current = load_edl_file(edl_path)
    candidates = load_candidates()
    media_by_asset = load_media()

    prior_notes: list[str] = []
    try:
        raw = json.loads(edl_path.read_text(encoding="utf-8"))
        prior_notes = list(raw.get("revision_notes") or [])
    except Exception:  # noqa: BLE001
        prior_notes = []

    emit(f"Revising {_rel(edl_path)} → v{version}")
    emit(f'  notes: "{notes}"')

    client = OpenAI(api_key=api_key, max_retries=0)
    revised = revise_edl(
        client,
        current=current,
        candidates=candidates,
        notes=notes,
        model=model,
        system_prompt=load_revise_prompt(),
        max_retries=DEFAULT_MAX_RETRIES,
        candidate_limit=None,
    )
    banned, end_with = parse_hard_constraints(*prior_notes, notes)
    if banned or end_with:
        revised = apply_hard_constraints(
            revised,
            banned=banned,
            end_with=end_with,
            candidates_assets={c.asset for c in candidates},
        )
        if banned:
            emit(f"  enforced ban: {', '.join(sorted(banned))}")
        if end_with:
            emit(f"  enforced ending: {end_with}")
    validated = validate_against_media(
        revised,
        media_by_asset=media_by_asset,
        candidates=candidates,
        clamp=not strict,
    )

    all_notes = [*prior_notes, notes]
    write_edl(
        validated,
        edl_out,
        version=version,
        revision_notes=all_notes,
    )

    clips = load_edl(edl_out)
    root = build_fcpxml(
        clips,
        load_fcpxml_media(),
        event_name="AI Travel Editor",
        project_name=project_name,
    )
    write_fcpxml(root, fcpxml_out)

    selected = sorted({c.asset for c in validated.edl.timeline})
    emit(f"  {len(validated.edl.timeline)} cuts · {validated.actual_duration:.1f}s · {len(selected)} assets")
    for warning in validated.warnings[:5]:
        emit(f"  warning: {warning}")
    emit()
    emit(f"✓ {_rel(edl_out)}")
    emit(f"✓ {_rel(fcpxml_out)}")
    return edl_out, fcpxml_out


def cmd_revise(args: argparse.Namespace) -> int:
    try:
        run_revise(
            edl_path=Path(args.edl),
            notes=args.notes,
            model=args.model,
            strict=args.strict,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .paths import ensure_app_home
    from .webapp.app import create_app

    ensure_app_home()
    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_desktop(args: argparse.Namespace) -> int:
    from .desktop import run_desktop

    port = None if not args.port else args.port
    return run_desktop(host=args.host, port=port)


def build_parser() -> argparse.ArgumentParser:
    cfg = load_settings()
    briefs = active_style_briefs()
    parser = argparse.ArgumentParser(
        prog="ai-edit",
        description="AI assistant editor for Final Cut Pro (create + conversational revise).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Create a new edit from an album export.")
    create.add_argument(
        "--album-export",
        type=Path,
        default=ROOT / "workspace" / "originals",
        help="Folder of exported originals (default: workspace/originals)",
    )
    create.add_argument(
        "--duration",
        type=float,
        default=float(cfg.get("default_duration", 90)),
        help="Target duration in seconds",
    )
    create.add_argument(
        "--style",
        default=str(cfg.get("default_style") or "cinematic"),
        choices=sorted(briefs),
        help="Editorial style preset",
    )
    create.add_argument(
        "--name",
        default=str(cfg.get("default_name") or "Lagos"),
        help='Project name (default from settings)',
    )
    create.add_argument("--model", default=str(cfg.get("model") or DEFAULT_MODEL))
    create.add_argument("--force", action="store_true", help="Regenerate proxies/analysis")
    create.add_argument(
        "--skip-analyze",
        action="store_true",
        help="Reuse existing vision analysis",
    )
    create.set_defaults(func=cmd_create)

    revise = sub.add_parser("revise", help="Revise an existing EDL with natural-language notes.")
    revise.add_argument("edl", type=Path, help="Path to an existing EDL JSON (e.g. lagos_v1.json)")
    revise.add_argument("notes", type=str, help="Revision request in plain English")
    revise.add_argument("--model", default=str(cfg.get("model") or DEFAULT_MODEL))
    revise.add_argument(
        "--strict",
        action="store_true",
        help="Fail instead of clamping invalid durations",
    )
    revise.set_defaults(func=cmd_revise)

    serve = sub.add_parser("serve", help="Run the local web UI in a browser.")
    serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8787, help="Bind port (default: 8787)")
    serve.set_defaults(func=cmd_serve)

    desktop = sub.add_parser("desktop", help="Open the native desktop app window (macOS).")
    desktop.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    desktop.add_argument(
        "--port",
        type=int,
        default=0,
        help="Bind port (0 = pick a free local port)",
    )
    desktop.set_defaults(func=cmd_desktop)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    code = args.func(args)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
