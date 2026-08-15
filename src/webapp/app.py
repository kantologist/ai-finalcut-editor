"""FastAPI application for the local AI Final Cut editor UI."""

from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from ..cli import run_revise
from ..inspect_media import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from ..main import (
    DEFAULT_INPUT,
    EDITS_DIR,
    OUTPUT_DIR,
    ROOT,
    active_style_briefs,
    detect_create_resume_stage,
    run_pipeline,
    slugify,
)
from ..settings import (
    list_model_options,
    list_prompts,
    load_settings,
    read_prompt,
    save_settings,
    settings_public,
    write_prompt,
)
from ..secrets import write_openai_api_key
from .jobs import Job, JobStatus, JobStore

WEBAPP_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(WEBAPP_DIR / "templates"))
STATIC_DIR = WEBAPP_DIR / "static"
FRAMES_DIR = ROOT / "workspace" / "frames"
THUMBS_DIR = ROOT / "workspace" / "proxies" / "thumbs"
MEDIA_JSON = ROOT / "workspace" / "metadata" / "media.json"
ANALYSIS_DIR = ROOT / "workspace" / "analysis"
CANDIDATES_PATH = ANALYSIS_DIR / "candidates.json"
STILLS_DIR = ROOT / "workspace" / "proxies" / "stills"

MEDIA_EXTENSIONS = {ext.lower() for ext in VIDEO_EXTENSIONS | IMAGE_EXTENSIONS}


class CreateRequest(BaseModel):
    name: str | None = None
    duration: float | None = None
    style: str | None = None
    album_export: str | None = None
    skip_analyze: bool = True
    force: bool = False
    resume: bool = False
    model: str | None = None


class ReviseRequest(BaseModel):
    edl: str
    notes: str = Field(min_length=1)
    model: str | None = None
    strict: bool = False
    resume: bool = False


class PromptUpdate(BaseModel):
    content: str


class SettingsUpdate(BaseModel):
    settings: dict[str, Any]
    openai_api_key: str | None = None


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _safe_under(base: Path, name: str) -> Path:
    candidate = (base / Path(name).name).resolve()
    if not str(candidate).startswith(str(base.resolve())):
        raise HTTPException(status_code=400, detail="Invalid path")
    return candidate


def _sanitize_filename(name: str) -> str:
    cleaned = Path(name).name.strip()
    if not cleaned or cleaned in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename")
    # Keep common media names intact; only reject null/path separators.
    if "/" in cleaned or "\\" in cleaned or "\x00" in cleaned:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return cleaned


def _media_kind(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    return None


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _load_media_index() -> dict[str, dict[str, Any]]:
    if not MEDIA_JSON.is_file():
        return {}
    try:
        rows = json.loads(MEDIA_JSON.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    by_name: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = Path(str(row.get("path") or ""))
        if path.name:
            by_name[path.name] = row
        media_id = str(row.get("id") or "")
        if media_id:
            by_name.setdefault(media_id, row)
    return by_name


def _frame_thumb_for(stem: str) -> Path | None:
    if not FRAMES_DIR.is_dir():
        return None
    exact = FRAMES_DIR / f"{stem}_0001.jpg"
    if exact.is_file():
        return exact
    matches = sorted(
        p
        for p in FRAMES_DIR.glob(f"{stem}_*.jpg")
        if " " not in p.name and p.is_file()
    )
    return matches[0] if matches else None


def _ensure_heic_thumb(path: Path) -> Path | None:
    """Convert HEIC → JPEG once for browser preview."""
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    out = THUMBS_DIR / f"{path.stem}.jpg"
    if out.is_file() and out.stat().st_mtime >= path.stat().st_mtime:
        return out
    try:
        subprocess.run(
            ["sips", "-s", "format", "jpeg", str(path), "--out", str(out)],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return out if out.is_file() else None
    return out if out.is_file() else None


def _list_media() -> list[dict[str, Any]]:
    originals = DEFAULT_INPUT
    if not originals.is_dir():
        return []
    index = _load_media_index()
    items: list[dict[str, Any]] = []
    for path in sorted(originals.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        kind = _media_kind(path)
        if kind is None:
            continue
        rel = path.relative_to(originals).as_posix()
        meta = index.get(path.name) or index.get(path.stem) or {}
        thumb = None
        if kind == "video":
            frame = _frame_thumb_for(path.stem)
            if frame is not None:
                thumb = f"/api/media/thumb/{quote(path.name)}"
        elif path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            thumb = f"/api/media/file/{quote(rel)}"
        elif path.suffix.lower() in {".heic", ".heif"}:
            thumb = f"/api/media/thumb/{quote(path.name)}"

        items.append(
            {
                "name": path.name,
                "rel": rel,
                "kind": kind,
                "ext": path.suffix.lower().lstrip("."),
                "size": path.stat().st_size,
                "size_label": _format_bytes(path.stat().st_size),
                "duration": meta.get("duration"),
                "width": meta.get("width"),
                "height": meta.get("height"),
                "thumb": thumb,
                "preview": f"/api/media/file/{quote(rel)}",
            }
        )
    return items


def _list_edits() -> list[dict[str, Any]]:
    if not EDITS_DIR.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(EDITS_DIR.glob("*_v*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        fcpxml = OUTPUT_DIR / f"{path.stem}.fcpxml"
        meta: dict[str, Any] = {
            "id": path.name,
            "stem": path.stem,
            "edl": _rel(path),
            "fcpxml": _rel(fcpxml) if fcpxml.is_file() else None,
            "mtime": path.stat().st_mtime,
        }
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            meta["title"] = raw.get("title") or path.stem
            meta["version"] = raw.get("version")
            timeline = raw.get("timeline") or []
            meta["cuts"] = len(timeline)
            meta["duration"] = round(
                sum(float(c.get("source_duration") or 0) for c in timeline),
                1,
            )
            notes = raw.get("revision_notes") or []
            meta["notes"] = notes[-1] if notes else None
        except Exception:  # noqa: BLE001
            meta["title"] = path.stem
        items.append(meta)
    return items


def _workspace_summary() -> dict[str, Any]:
    originals = DEFAULT_INPUT
    media = _list_media()
    analysis = ROOT / "workspace" / "analysis"
    analysis_count = 0
    if analysis.is_dir():
        analysis_count = sum(
            1 for p in analysis.glob("*.json") if p.name != "candidates.json"
        )
    videos = sum(1 for m in media if m["kind"] == "video")
    photos = sum(1 for m in media if m["kind"] == "image")
    cfg = load_settings()
    briefs = active_style_briefs()
    return {
        "originals_dir": _rel(originals),
        "originals_count": len(media),
        "videos_count": videos,
        "photos_count": photos,
        "analysis_count": analysis_count,
        "styles": sorted(briefs),
        "edits": _list_edits(),
        "media": media,
        "config": cfg,
        "prompts": list_prompts(),
        "models": list_model_options(str(cfg.get("model") or "")),
    }


async def _save_uploads(
    files: list[UploadFile],
    *,
    replace: bool,
) -> dict[str, Any]:
    DEFAULT_INPUT.mkdir(parents=True, exist_ok=True)
    saved = 0
    skipped = 0
    rejected = 0
    names: list[str] = []

    for upload in files:
        raw_name = upload.filename or ""
        # Folder picks arrive as "Album/clip.MOV" via webkitdirectory.
        candidate = Path(raw_name.replace("\\", "/")).name
        try:
            filename = _sanitize_filename(candidate)
        except HTTPException:
            rejected += 1
            continue
        suffix = Path(filename).suffix.lower()
        if suffix not in MEDIA_EXTENSIONS:
            rejected += 1
            continue

        dest = DEFAULT_INPUT / filename
        if dest.exists() and not replace:
            skipped += 1
            continue

        with dest.open("wb") as handle:
            shutil.copyfileobj(upload.file, handle)
        saved += 1
        names.append(filename)

    return {
        "saved": saved,
        "skipped": skipped,
        "rejected": rejected,
        "files": names,
        "media": _list_media(),
        "originals_count": len(_list_media()),
        "videos_count": sum(1 for m in _list_media() if m["kind"] == "video"),
        "photos_count": sum(1 for m in _list_media() if m["kind"] == "image"),
    }


def _delete_media_asset(rel_path: str) -> dict[str, Any]:
    base = DEFAULT_INPUT.resolve()
    target = (DEFAULT_INPUT / rel_path).resolve()
    if not str(target).startswith(str(base)) or not target.is_file():
        raise HTTPException(status_code=404, detail="Media not found")
    if _media_kind(target) is None:
        raise HTTPException(status_code=400, detail="Unsupported media type")

    stem = target.stem
    name = target.name
    removed: list[str] = [str(target.relative_to(base))]

    target.unlink()

    if FRAMES_DIR.is_dir():
        for frame in FRAMES_DIR.glob(f"{stem}_*.jpg"):
            frame.unlink(missing_ok=True)
            removed.append(_rel(frame))

    analysis = ANALYSIS_DIR / f"{stem}.json"
    if analysis.is_file():
        analysis.unlink()
        removed.append(_rel(analysis))

    thumb = THUMBS_DIR / f"{stem}.jpg"
    if thumb.is_file():
        thumb.unlink()
        removed.append(_rel(thumb))

    if STILLS_DIR.is_dir():
        for still in STILLS_DIR.glob(f"{stem}.*"):
            still.unlink(missing_ok=True)
            removed.append(_rel(still))

    if MEDIA_JSON.is_file():
        try:
            rows = json.loads(MEDIA_JSON.read_text(encoding="utf-8"))
            filtered = [
                row
                for row in rows
                if Path(str(row.get("path") or "")).name != name and str(row.get("id") or "") != stem
            ]
            if len(filtered) != len(rows):
                MEDIA_JSON.write_text(json.dumps(filtered, indent=2) + "\n", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    if CANDIDATES_PATH.is_file():
        try:
            rows = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
            filtered = [row for row in rows if str(row.get("asset") or "") != name]
            if len(filtered) != len(rows):
                CANDIDATES_PATH.write_text(json.dumps(filtered, indent=2) + "\n", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    items = _list_media()
    return {
        "deleted": name,
        "removed": removed,
        "media": items,
        "originals_count": len(items),
        "videos_count": sum(1 for m in items if m["kind"] == "video"),
        "photos_count": sum(1 for m in items if m["kind"] == "image"),
    }


def _clear_output_files(*, edl_name: str | None = None) -> dict[str, Any]:
    """Delete EDL JSON and/or FCPXML outputs. If edl_name is set, delete that pair only."""
    removed: list[str] = []

    if edl_name is not None:
        edl_path = _safe_under(EDITS_DIR, edl_name)
        if not edl_path.is_file() or edl_path.suffix.lower() != ".json":
            raise HTTPException(status_code=404, detail="EDL not found")
        stem = edl_path.stem
        edl_path.unlink()
        removed.append(_rel(edl_path))
        fcpxml = OUTPUT_DIR / f"{stem}.fcpxml"
        if fcpxml.is_file():
            fcpxml.unlink()
            removed.append(_rel(fcpxml))
    else:
        if EDITS_DIR.is_dir():
            for path in EDITS_DIR.glob("*.json"):
                path.unlink()
                removed.append(_rel(path))
        if OUTPUT_DIR.is_dir():
            for path in OUTPUT_DIR.glob("*.fcpxml"):
                path.unlink()
                removed.append(_rel(path))

    return {
        "removed": removed,
        "count": len(removed),
        "edits": _list_edits(),
    }


def create_app() -> FastAPI:
    store = JobStore()
    app = FastAPI(title="AI Final Cut", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {"summary": _workspace_summary()},
        )

    @app.get("/api/workspace")
    def workspace() -> dict[str, Any]:
        return _workspace_summary()

    @app.get("/api/edits")
    def edits() -> dict[str, Any]:
        return {"edits": _list_edits()}

    @app.delete("/api/edits/{name}")
    def delete_edit(name: str) -> dict[str, Any]:
        return _clear_output_files(edl_name=name)

    @app.delete("/api/outputs")
    def clear_outputs() -> dict[str, Any]:
        return _clear_output_files()

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return settings_public()

    @app.put("/api/settings")
    def put_settings(body: SettingsUpdate) -> dict[str, Any]:
        try:
            saved = save_settings(body.settings)
            if body.openai_api_key is not None:
                write_openai_api_key(body.openai_api_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return settings_public() | {"settings": saved, "settings_path": "workspace/settings.json"}

    @app.get("/api/prompts")
    def prompts() -> dict[str, Any]:
        return {"prompts": list_prompts()}

    @app.get("/api/prompts/{name}")
    def get_prompt(name: str) -> dict[str, Any]:
        try:
            content = read_prompt(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        meta = next((p for p in list_prompts() if p["id"] == name), None)
        return {"id": name, "label": (meta or {}).get("label", name), "path": (meta or {}).get("path"), "content": content}

    @app.put("/api/prompts/{name}")
    def put_prompt(name: str, body: PromptUpdate) -> dict[str, Any]:
        try:
            content = write_prompt(name, body.content)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": name, "content": content, "ok": True}

    @app.get("/api/media")
    def media_list() -> dict[str, Any]:
        items = _list_media()
        return {
            "media": items,
            "originals_count": len(items),
            "videos_count": sum(1 for m in items if m["kind"] == "video"),
            "photos_count": sum(1 for m in items if m["kind"] == "image"),
            "originals_dir": _rel(DEFAULT_INPUT),
        }

    @app.post("/api/media/upload")
    async def media_upload(
        files: list[UploadFile] = File(...),
        replace: bool = False,
    ) -> dict[str, Any]:
        if not files:
            raise HTTPException(status_code=400, detail="No files uploaded")
        return await _save_uploads(files, replace=replace)

    @app.delete("/api/media/{rel_path:path}")
    def media_delete(rel_path: str) -> dict[str, Any]:
        return _delete_media_asset(rel_path)

    @app.get("/api/media/file/{rel_path:path}")
    def media_file(rel_path: str) -> FileResponse:
        base = DEFAULT_INPUT.resolve()
        target = (DEFAULT_INPUT / rel_path).resolve()
        if not str(target).startswith(str(base)) or not target.is_file():
            raise HTTPException(status_code=404, detail="Media not found")
        if _media_kind(target) is None:
            raise HTTPException(status_code=400, detail="Unsupported media type")
        mime, _ = mimetypes.guess_type(str(target))
        return FileResponse(target, media_type=mime or "application/octet-stream")

    @app.get("/api/media/thumb/{name}")
    def media_thumb(name: str) -> FileResponse:
        path = _safe_under(DEFAULT_INPUT, name)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Media not found")
        kind = _media_kind(path)
        thumb: Path | None = None
        if kind == "video":
            thumb = _frame_thumb_for(path.stem)
        elif path.suffix.lower() in {".heic", ".heif"}:
            thumb = _ensure_heic_thumb(path)
        elif path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            thumb = path
        if thumb is None or not thumb.is_file():
            raise HTTPException(status_code=404, detail="Thumbnail not available")
        return FileResponse(thumb, media_type="image/jpeg")

    @app.post("/api/create")
    def create(body: CreateRequest) -> dict[str, Any]:
        cfg = load_settings()
        briefs = active_style_briefs()
        name = (body.name or str(cfg.get("default_name") or "Lagos")).strip() or "Lagos"
        duration = float(body.duration if body.duration is not None else cfg.get("default_duration", 90))
        style = (body.style or str(cfg.get("default_style") or "cinematic")).strip()
        model = (body.model or str(cfg.get("model") or "gpt-5.4")).strip()
        if style not in briefs:
            raise HTTPException(status_code=400, detail=f"Unknown style: {style}")
        if duration <= 5 or duration > 600:
            raise HTTPException(status_code=400, detail="Duration must be between 5 and 600 seconds")
        album = Path(body.album_export) if body.album_export else DEFAULT_INPUT
        if not album.is_dir():
            raise HTTPException(status_code=400, detail=f"Album folder not found: {album}")

        resume = bool(body.resume) and not bool(body.force)
        slug = slugify(name)
        resume_from = detect_create_resume_stage(slug=slug, force=False) if resume else None
        request_payload = {
            "name": name,
            "duration": duration,
            "style": style,
            "model": model,
            "album_export": str(album),
            "skip_analyze": body.skip_analyze,
            "force": body.force,
            "resume": resume,
        }

        def runner(job: Job) -> None:
            def on_progress(msg: str) -> None:
                if msg:
                    job.append(msg)

            output = run_pipeline(
                input_dir=album,
                duration=duration,
                style=style,
                name=name,
                force=body.force,
                skip_analyze=body.skip_analyze,
                resume=resume,
                model=model,
                on_progress=on_progress,
            )
            edl = EDITS_DIR / f"{slug}_v1.json"
            job.finish(
                status=JobStatus.succeeded,
                result={
                    "edl": _rel(edl),
                    "fcpxml": _rel(output),
                    "stem": edl.stem,
                },
            )

        job = store.start(
            "create",
            runner,
            request=request_payload,
            resume_from=None if resume_from in (None, "done") else resume_from,
        )
        return {
            "job_id": job.id,
            "kind": job.kind,
            "resume_from": job.resume_from,
        }

    @app.post("/api/revise")
    def revise(body: ReviseRequest) -> dict[str, Any]:
        cfg = load_settings()
        model = (body.model or str(cfg.get("model") or "gpt-5.4")).strip()
        edl_path = Path(body.edl)
        if not edl_path.is_absolute():
            # Accept bare filenames from the edits library.
            candidate = EDITS_DIR / edl_path.name
            edl_path = candidate if candidate.is_file() else (ROOT / edl_path)
        if not edl_path.is_file():
            raise HTTPException(status_code=404, detail=f"EDL not found: {body.edl}")

        request_payload = {
            "edl": str(edl_path.name),
            "notes": body.notes,
            "model": model,
            "strict": body.strict,
            "resume": bool(body.resume),
        }

        def runner(job: Job) -> None:
            def on_progress(msg: str) -> None:
                if msg:
                    job.append(msg)

            edl_out, fcpxml_out = run_revise(
                edl_path=edl_path,
                notes=body.notes,
                model=model,
                strict=body.strict,
                resume=bool(body.resume),
                on_progress=on_progress,
            )
            job.finish(
                status=JobStatus.succeeded,
                result={
                    "edl": _rel(edl_out),
                    "fcpxml": _rel(fcpxml_out),
                    "stem": edl_out.stem,
                },
            )

        job = store.start("revise", runner, request=request_payload)
        return {"job_id": job.id, "kind": job.kind}

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str) -> dict[str, Any]:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status != JobStatus.failed:
            raise HTTPException(status_code=400, detail="Only failed jobs can be retried")
        if not job.request:
            raise HTTPException(status_code=400, detail="Job has no saved request to retry")

        if job.kind == "create":
            payload = CreateRequest(
                name=job.request.get("name"),
                duration=job.request.get("duration"),
                style=job.request.get("style"),
                album_export=job.request.get("album_export"),
                model=job.request.get("model"),
                skip_analyze=True,
                force=False,
                resume=True,
            )
            return create(payload)

        if job.kind == "revise":
            payload = ReviseRequest(
                edl=str(job.request.get("edl") or ""),
                notes=str(job.request.get("notes") or ""),
                model=job.request.get("model"),
                strict=bool(job.request.get("strict")),
                resume=True,
            )
            return revise(payload)

        raise HTTPException(status_code=400, detail=f"Unsupported job kind: {job.kind}")

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return {
            "job_id": job.id,
            "kind": job.kind,
            "status": job.status.value,
            "lines": job.lines,
            "result": job.result,
            "error": job.error,
            "request": job.request,
            "resume_from": job.resume_from,
            "can_retry": job.status == JobStatus.failed and bool(job.request),
        }

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job_id: str) -> StreamingResponse:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")

        def event_stream():
            for payload in store.iter_sse(job):
                yield f"data: {json.dumps(payload)}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/download/edl/{name}")
    def download_edl(name: str) -> FileResponse:
        path = _safe_under(EDITS_DIR, name)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="EDL not found")
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    @app.get("/api/download/fcpxml/{name}")
    def download_fcpxml(name: str) -> FileResponse:
        path = _safe_under(OUTPUT_DIR, name)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="FCPXML not found")
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    return app
