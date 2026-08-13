# Security Policy

## Reporting a vulnerability

Please open a private GitHub security advisory on this repository, or email the maintainer listed in `pyproject.toml`.

Do **not** open a public issue for secret leaks or remote code execution reports.

## Scope notes

- This app runs a **local** HTTP server bound to `127.0.0.1` by default.
- API keys live in `.env` / Application Support — never commit them.
- Treat generated FCPXML and media paths as trusted local files only.
