"""Editable prompts + pipeline settings for the local app."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .paths import ROOT

PROMPTS_DIR = ROOT / "prompts"
SETTINGS_PATH = ROOT / "workspace" / "settings.json"

PROMPT_FILES: dict[str, str] = {
    "editor": "editor.md",
    "vision": "vision.md",
    "revise": "revise.md",
}

PROMPT_LABELS: dict[str, str] = {
    "editor": "Editor brief",
    "vision": "Vision analysis",
    "revise": "Revision",
}

DEFAULT_STYLE_BRIEFS: dict[str, str] = {
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

DEFAULT_SCORE_WEIGHTS: dict[str, float] = {
    "visual_interest": 0.30,
    "composition": 0.20,
    "stability": 0.15,
    "story_value": 0.15,
    "uniqueness": 0.10,
    "user_priority": 0.10,
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "model": "gpt-5.4",
    "default_name": "Travel Edit",
    "default_duration": 90.0,
    "default_style": "cinematic",
    "strong_score": 0.65,
    "max_video_source_duration": 6.0,
    "default_still_duration": 2.5,
    "vision_batch_size": 8,
    "request_pause_sec": 1.0,
    "max_retries": 12,
    "spatial_conform": "fill_vertical_fit_wide",
    "sequence_width": 1080,
    "sequence_height": 1920,
    "score_weights": dict(DEFAULT_SCORE_WEIGHTS),
    "style_briefs": dict(DEFAULT_STYLE_BRIEFS),
}

# Curated models for vision analysis + EDL planning.
# Scores are relative to this app's task (1–10). Costs are approximate USD
# list prices and rough per-job estimates (full create ≈ 30 clips vision + 1 plan;
# revise ≈ 1 plan). Update if OpenAI pricing changes.
MODEL_CATALOG: list[dict[str, Any]] = [
    {
        "id": "gpt-5.6-sol",
        "label": "GPT-5.6 Sol",
        "task_score": 9.7,
        "vision_score": 9.6,
        "edit_score": 9.8,
        "cost_tier": "$$$$",
        "input_per_mtok": 5.00,
        "output_per_mtok": 20.00,
        "est_create_usd": 4.50,
        "est_revise_usd": 0.35,
        "blurb": "Top intelligence for hard framing/story calls; highest cost.",
    },
    {
        "id": "gpt-5.5",
        "label": "GPT-5.5",
        "task_score": 9.5,
        "vision_score": 9.7,
        "edit_score": 9.3,
        "cost_tier": "$$$$",
        "input_per_mtok": 5.00,
        "output_per_mtok": 20.00,
        "est_create_usd": 4.20,
        "est_revise_usd": 0.32,
        "blurb": "Best vision detail; strong when clip descriptions must be precise.",
    },
    {
        "id": "gpt-5.4",
        "label": "GPT-5.4",
        "task_score": 9.1,
        "vision_score": 9.2,
        "edit_score": 9.0,
        "cost_tier": "$$$",
        "input_per_mtok": 2.50,
        "output_per_mtok": 10.00,
        "est_create_usd": 2.10,
        "est_revise_usd": 0.18,
        "blurb": "Recommended default — strong vision + planning balance.",
        "recommended": True,
    },
    {
        "id": "gpt-5.6-terra",
        "label": "GPT-5.6 Terra",
        "task_score": 8.9,
        "vision_score": 8.8,
        "edit_score": 9.0,
        "cost_tier": "$$$",
        "input_per_mtok": 2.50,
        "output_per_mtok": 10.00,
        "est_create_usd": 2.00,
        "est_revise_usd": 0.17,
        "blurb": "Balanced GPT-5.6 tier for intelligence vs spend.",
    },
    {
        "id": "gpt-5",
        "label": "GPT-5",
        "task_score": 8.6,
        "vision_score": 8.5,
        "edit_score": 8.7,
        "cost_tier": "$$$",
        "input_per_mtok": 1.25,
        "output_per_mtok": 10.00,
        "est_create_usd": 1.60,
        "est_revise_usd": 0.14,
        "blurb": "Capable all-rounder; slightly behind 5.4 on vision nuance.",
    },
    {
        "id": "gpt-5.4-mini",
        "label": "GPT-5.4 Mini",
        "task_score": 7.8,
        "vision_score": 7.6,
        "edit_score": 8.0,
        "cost_tier": "$$",
        "input_per_mtok": 0.40,
        "output_per_mtok": 1.60,
        "est_create_usd": 0.45,
        "est_revise_usd": 0.04,
        "blurb": "Good budget upgrade over 4o-mini for this pipeline.",
    },
    {
        "id": "gpt-5.6-luna",
        "label": "GPT-5.6 Luna",
        "task_score": 7.5,
        "vision_score": 7.3,
        "edit_score": 7.7,
        "cost_tier": "$$",
        "input_per_mtok": 0.35,
        "output_per_mtok": 1.40,
        "est_create_usd": 0.38,
        "est_revise_usd": 0.03,
        "blurb": "Cost-sensitive 5.6 tier; fine for revise-heavy workflows.",
    },
    {
        "id": "gpt-4.1",
        "label": "GPT-4.1",
        "task_score": 8.0,
        "vision_score": 8.1,
        "edit_score": 7.9,
        "cost_tier": "$$$",
        "input_per_mtok": 2.00,
        "output_per_mtok": 8.00,
        "est_create_usd": 1.50,
        "est_revise_usd": 0.12,
        "blurb": "Solid vision; prefer 5.4 if available.",
    },
    {
        "id": "gpt-4.1-mini",
        "label": "GPT-4.1 Mini",
        "task_score": 7.0,
        "vision_score": 7.0,
        "edit_score": 7.0,
        "cost_tier": "$$",
        "input_per_mtok": 0.40,
        "output_per_mtok": 1.60,
        "est_create_usd": 0.40,
        "est_revise_usd": 0.03,
        "blurb": "Cheap and competent for drafts / skip-analyze revises.",
    },
    {
        "id": "gpt-4o",
        "label": "GPT-4o",
        "task_score": 7.4,
        "vision_score": 7.5,
        "edit_score": 7.3,
        "cost_tier": "$$",
        "input_per_mtok": 2.50,
        "output_per_mtok": 10.00,
        "est_create_usd": 1.40,
        "est_revise_usd": 0.12,
        "blurb": "Previous multimodal default; outclassed by GPT-5.x for this task.",
    },
    {
        "id": "gpt-4o-mini",
        "label": "GPT-4o Mini",
        "task_score": 5.8,
        "vision_score": 5.5,
        "edit_score": 6.2,
        "cost_tier": "$",
        "input_per_mtok": 0.15,
        "output_per_mtok": 0.60,
        "est_create_usd": 0.18,
        "est_revise_usd": 0.02,
        "blurb": "Lowest cost; weaker scene judgment and more invented assets.",
    },
    {
        "id": "gpt-5.4-nano",
        "label": "GPT-5.4 Nano",
        "task_score": 6.2,
        "vision_score": 5.8,
        "edit_score": 6.5,
        "cost_tier": "$",
        "input_per_mtok": 0.10,
        "output_per_mtok": 0.40,
        "est_create_usd": 0.12,
        "est_revise_usd": 0.015,
        "blurb": "Fast/cheap experiments only — not ideal for final cuts.",
    },
]


def list_model_options(selected: str | None = None) -> list[dict[str, Any]]:
    selected = (selected or "").strip()
    options = [deepcopy(row) for row in MODEL_CATALOG]
    known = {row["id"] for row in options}
    if selected and selected not in known:
        options.insert(
            0,
            {
                "id": selected,
                "label": selected,
                "task_score": None,
                "vision_score": None,
                "edit_score": None,
                "cost_tier": "?",
                "input_per_mtok": None,
                "output_per_mtok": None,
                "est_create_usd": None,
                "est_revise_usd": None,
                "blurb": "Custom / unlisted model id from settings.",
            },
        )
    for row in options:
        row["selected"] = row["id"] == selected
    return options


def model_option(model_id: str) -> dict[str, Any] | None:
    for row in list_model_options(model_id):
        if row["id"] == model_id:
            return row
    return None


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.is_file():
        return deepcopy(DEFAULT_SETTINGS)
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return deepcopy(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return deepcopy(DEFAULT_SETTINGS)
    return _deep_merge(DEFAULT_SETTINGS, raw)


def validate_settings(payload: dict[str, Any]) -> dict[str, Any]:
    data = _deep_merge(DEFAULT_SETTINGS, payload)

    model = str(data.get("model") or "").strip()
    if not model:
        raise ValueError("model is required")
    data["model"] = model

    data["default_name"] = str(data.get("default_name") or "Lagos").strip() or "Lagos"

    duration = float(data["default_duration"])
    if duration < 10 or duration > 600:
        raise ValueError("default_duration must be between 10 and 600")
    data["default_duration"] = duration

    style = str(data.get("default_style") or "cinematic").strip()
    briefs = data.get("style_briefs") or {}
    if not isinstance(briefs, dict) or not briefs:
        raise ValueError("style_briefs must be a non-empty object")
    data["style_briefs"] = {str(k): str(v) for k, v in briefs.items()}
    if style not in data["style_briefs"]:
        raise ValueError(f"default_style must be one of: {', '.join(sorted(data['style_briefs']))}")
    data["default_style"] = style

    strong = float(data["strong_score"])
    if not 0.0 <= strong <= 1.0:
        raise ValueError("strong_score must be between 0 and 1")
    data["strong_score"] = strong

    max_video = float(data["max_video_source_duration"])
    if max_video <= 0 or max_video > 30:
        raise ValueError("max_video_source_duration must be between 0 and 30")
    data["max_video_source_duration"] = max_video

    still = float(data["default_still_duration"])
    if still <= 0 or still > 30:
        raise ValueError("default_still_duration must be between 0 and 30")
    data["default_still_duration"] = still

    batch = int(data["vision_batch_size"])
    if batch < 1 or batch > 32:
        raise ValueError("vision_batch_size must be between 1 and 32")
    data["vision_batch_size"] = batch

    pause = float(data["request_pause_sec"])
    if pause < 0 or pause > 60:
        raise ValueError("request_pause_sec must be between 0 and 60")
    data["request_pause_sec"] = pause

    retries = int(data["max_retries"])
    if retries < 0 or retries > 50:
        raise ValueError("max_retries must be between 0 and 50")
    data["max_retries"] = retries

    conform = str(data.get("spatial_conform") or "fill_vertical_fit_wide").strip().lower()
    if conform not in {"fill", "fit", "fill_vertical_fit_wide"}:
        raise ValueError(
            "spatial_conform must be 'fill', 'fit', or 'fill_vertical_fit_wide'"
        )
    data["spatial_conform"] = conform

    seq_w = int(data.get("sequence_width", 1080))
    seq_h = int(data.get("sequence_height", 1920))
    if seq_w < 16 or seq_w > 8192 or seq_h < 16 or seq_h > 8192:
        raise ValueError("sequence_width/height must be between 16 and 8192")
    data["sequence_width"] = seq_w
    data["sequence_height"] = seq_h

    weights = data.get("score_weights") or {}
    if not isinstance(weights, dict):
        raise ValueError("score_weights must be an object")
    cleaned_weights: dict[str, float] = {}
    for key in DEFAULT_SCORE_WEIGHTS:
        if key not in weights:
            raise ValueError(f"score_weights missing '{key}'")
        value = float(weights[key])
        if value < 0:
            raise ValueError(f"score_weights.{key} must be >= 0")
        cleaned_weights[key] = value
    total = sum(cleaned_weights.values())
    if abs(total - 1.0) > 0.05:
        raise ValueError(f"score_weights should sum to ~1.0 (got {total:.3f})")
    data["score_weights"] = cleaned_weights

    return data


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    data = validate_settings(payload)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


def prompt_path(name: str) -> Path:
    filename = PROMPT_FILES.get(name)
    if filename is None:
        raise KeyError(f"Unknown prompt: {name}")
    return PROMPTS_DIR / filename


def list_prompts() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key, filename in PROMPT_FILES.items():
        path = PROMPTS_DIR / filename
        items.append(
            {
                "id": key,
                "label": PROMPT_LABELS.get(key, key),
                "path": f"prompts/{filename}",
                "exists": path.is_file(),
                "chars": path.stat().st_size if path.is_file() else 0,
            }
        )
    return items


def read_prompt(name: str) -> str:
    path = prompt_path(name)
    if not path.is_file():
        raise FileNotFoundError(f"Missing prompt file: {path}")
    return path.read_text(encoding="utf-8")


def write_prompt(name: str, content: str) -> str:
    text = content.replace("\r\n", "\n")
    if not text.strip():
        raise ValueError("Prompt cannot be empty")
    path = prompt_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")
    return text


def settings_public() -> dict[str, Any]:
    from .secrets import mask_openai_api_key, read_openai_api_key

    settings = load_settings()
    api_key = read_openai_api_key()
    return {
        "settings": settings,
        "settings_path": "workspace/settings.json",
        "prompts": list_prompts(),
        "defaults": deepcopy(DEFAULT_SETTINGS),
        "models": list_model_options(str(settings.get("model") or "")),
        "openai_api_key": api_key,
        "openai_api_key_masked": mask_openai_api_key(api_key),
        "openai_api_key_set": bool(api_key),
    }
