# Contributing

Thanks for helping improve AI Final Cut Editor.

## Development setup

```bash
uv sync --extra dev
cp .env.example .env   # add OPENAI_API_KEY for live API tests
uv run ai-edit serve
```

System deps: `ffmpeg` on `PATH` (`brew install ffmpeg`).

## Guidelines

- Keep FCPXML generation **deterministic** — no LLM output in XML.
- Prefer small, focused PRs (pipeline stage, UI, packaging).
- Do not commit `.env`, media under `workspace/originals`, or generated analysis/frames.
- Match existing code style; avoid drive-by refactors.

## Desktop / packaging

```bash
uv sync --extra desktop
uv run ai-edit desktop
./packaging/build_macos.sh
```

## License

By contributing, you agree your work is released under the MIT License.
