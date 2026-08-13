# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the macOS .app bundle."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parent
block_cipher = None

webview_datas, webview_binaries, webview_hidden = collect_all("webview")

datas = [
    (str(ROOT / "prompts"), "prompts"),
    (str(ROOT / "packaging" / "bundle_workspace"), "workspace"),
    (str(ROOT / "src" / "webapp" / "static"), "src/webapp/static"),
    (str(ROOT / "src" / "webapp" / "templates"), "src/webapp/templates"),
] + webview_datas

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "multipart",
    "src",
    "src.cli",
    "src.desktop",
    "src.webapp.app",
    "src.webapp.jobs",
] + webview_hidden

a = Analysis(
    [str(ROOT / "packaging" / "macos_entry.py")],
    pathex=[str(ROOT)],
    binaries=webview_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AI Final Cut Editor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AI Final Cut Editor",
)

app = BUNDLE(
    coll,
    name="AI Final Cut Editor.app",
    icon=str(ROOT / "packaging" / "AppIcon.icns") if (ROOT / "packaging" / "AppIcon.icns").exists() else None,
    bundle_identifier="com.aifinalcuteditor.app",
    info_plist={
        "CFBundleName": "AI Final Cut Editor",
        "CFBundleDisplayName": "AI Final Cut Editor",
        "CFBundleShortVersionString": "0.2.0",
        "CFBundleVersion": "0.2.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        "LSMinimumSystemVersion": "12.0",
    },
)
