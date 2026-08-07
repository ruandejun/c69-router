# -*- mode: python ; coding: utf-8 -*-
"""
c69-router.spec — Cross-platform PyInstaller spec
Auto-detects OS: Windows → c69-router.exe | Linux/macOS → c69-router
"""
import sys
import os

_is_win = sys.platform == "win32"
_is_mac = sys.platform == "darwin"

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('static', 'static'),
    ],
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'fastapi',
        'pydantic_core._pydantic_core',
        'websockets',
        'app.error_reporter',
        'app.platform',
        'app.platform.windows' if _is_win else ('app.platform.macos' if _is_mac else 'app.platform.linux'),
        'requests',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='c69-router',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=_is_mac,   # macOS: enable argv emulation for proper arg handling
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
