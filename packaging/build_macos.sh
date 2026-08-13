#!/usr/bin/env bash
# Build a macOS .app (and optional .dmg) for AI Final Cut Editor.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Syncing build dependencies"
uv sync --extra dev

echo "==> Cleaning previous build"
rm -rf build dist/macos

echo "==> Running PyInstaller"
uv run pyinstaller packaging/macos.spec --noconfirm --distpath dist/macos --workpath build/pyinstaller

APP="dist/macos/AI Final Cut Editor.app"
if [[ ! -d "$APP" ]]; then
  echo "Build failed: $APP not found" >&2
  exit 1
fi

# Clear Finder metadata so ad-hoc codesign succeeds on local builds.
xattr -cr "$APP" 2>/dev/null || true
codesign --force --deep -s - "$APP" 2>/dev/null || true

echo "==> App bundle ready: $APP"

if command -v hdiutil >/dev/null 2>&1; then
  DMG="dist/macos/AI-Final-Cut-Editor.dmg"
  echo "==> Creating DMG: $DMG"
  rm -f "$DMG"
  hdiutil create \
    -volname "AI Final Cut Editor" \
    -srcfolder "$APP" \
    -ov -format UDZO \
    "$DMG"
  echo "==> DMG ready: $DMG"
fi

echo "Done. Drag the .app into /Applications, or distribute the .dmg."
