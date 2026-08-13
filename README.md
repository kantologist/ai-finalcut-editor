# AI Final Cut Editor

Local **AI assistant editor** for [Final Cut Pro](https://www.apple.com/final-cut-pro/).
Upload a travel album, get a vertical-first EDL + FCPXML you can import into FCP.

Runs entirely on your Mac: vision analysis + edit planning via OpenAI, deterministic FCPXML export in Python.

**[Download the Mac app](https://github.com/kantologist/ai-finalcut-editor/releases/latest/download/AI-Final-Cut-Editor.dmg)** · [Project site](https://kantologist.github.io/ai-finalcut-editor/) · [Releases](https://github.com/kantologist/ai-finalcut-editor/releases/latest)

---

## Features

- **Desktop app** (native WebKit window) or browser UI / CLI
- Vision analysis of proxy frames → ranked candidate shots
- LLM Edit Decision List with hard validation (no invented assets)
- Deterministic **FCPXML** export (9:16, Fill for portrait / Fit for landscape)
- Create, revise, resume failed jobs from the last completed stage
- Editable prompts + model picker with task scores / cost estimates

## Download (macOS)

1. Get the latest installer: **[AI-Final-Cut-Editor.dmg](https://github.com/kantologist/ai-finalcut-editor/releases/latest/download/AI-Final-Cut-Editor.dmg)**
2. Open the disk image and drag **AI Final Cut Editor** into Applications.
3. First launch may need **Right-click → Open** (the build is unsigned until you notarize it).
4. Add your OpenAI API key in **Settings → API key**. Install [ffmpeg](https://ffmpeg.org/) (`brew install ffmpeg`).

Library data lives in `~/Library/Application Support/AIFinalCutEditor/`.

## Requirements

| Tool | Notes |
|------|--------|
| macOS 12+ | Desktop app & FCPXML workflow |
| Python 3.10+ | [uv](https://github.com/astral-sh/uv) recommended (developers) |
| [ffmpeg](https://ffmpeg.org/) | Proxy frames + still→MOV (`brew install ffmpeg`) |
| OpenAI API key | Vision + edit planning |

## Quick start (developers)

```bash
git clone https://github.com/kantologist/ai-finalcut-editor.git
cd ai-finalcut-editor
uv sync --extra desktop

cp .env.example .env
# edit .env and set OPENAI_API_KEY=sk-...

# Native desktop window (recommended)
uv run ai-edit desktop

# Or browser UI
uv run ai-edit serve
# open http://127.0.0.1:8787
```

CLI:

```bash
uv run ai-edit create --album-export workspace/originals --duration 90 --name "Travel Edit"
uv run ai-edit revise workspace/edits/travel_edit_v1.json "Longer holds, end on the ocean."
```

## Build a `.app` / `.dmg` yourself

```bash
chmod +x packaging/build_macos.sh
./packaging/build_macos.sh
```

Artifacts:

- `dist/macos/AI Final Cut Editor.app`
- `dist/macos/AI-Final-Cut-Editor.dmg`

Override the data directory anytime with:

```bash
export AI_EDIT_HOME=~/Movies/AIFinalCut
uv run ai-edit desktop
```

## Project layout

```
prompts/          Editor, vision, revise briefs
src/              Pipeline + FastAPI UI + desktop shell
workspace/        Local media, analysis, EDLs, FCPXML
packaging/        macOS PyInstaller spec + build script
docs/             GitHub Pages site
```

Pipeline: **inspect → proxy frames → vision → candidates → EDL → FCPXML**.

## Configuration

- UI: **Settings** (frame / aspect, API key, model, duration, style, pacing)
- Files: `workspace/settings.json`, `prompts/*.md`
- Secrets: `.env` (`OPENAI_API_KEY` only — never commit)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and PRs welcome.

## License

[MIT](LICENSE)
