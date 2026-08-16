# -*- mode: python ; coding: utf-8 -*-
"""
c69-router.spec — Cross-platform PyInstaller spec
  Windows → c69-router.exe
  Linux   → c69-router  (binary)
  macOS   → c69-router.app (double-click app bundle + osascript password popup)
"""
import sys
import os

_is_win = sys.platform == "win32"
_is_mac = sys.platform == "darwin"
_is_lin = not _is_win and not _is_mac

# Platform-specific hidden imports
_platform_hidden = []
if _is_win:
    _platform_hidden = ['app.platform.windows']
elif _is_mac:
    _platform_hidden = ['app.platform.macos']
else:
    _platform_hidden = ['app.platform.linux']

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[
        ('mihomo.exe', '.'),
        ('sing-box.exe', '.'),
        ('wintun.dll', '.'),
        ('geoip.metadb', '.'),
    ] if _is_win else [],
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
        'requests',
        'yaml',
    ] + _platform_hidden,
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
    argv_emulation=_is_mac,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# macOS only: wrap EXE into a .app bundle so users can double-click
if _is_mac:
    app = BUNDLE(
        exe,
        name='c69-router.app',
        icon=None,           # TODO: set icon='assets/icon.icns' if available
        bundle_identifier='com.c69.router',
        info_plist={
            'CFBundleName': 'c69-Router',
            'CFBundleDisplayName': 'c69-Router',
            'CFBundleVersion': '2.0.0',
            'CFBundleShortVersionString': '2.0',
            'NSHighResolutionCapable': True,
            # Allow incoming network connections (needed for web UI + DHCP)
            'NSAppTransportSecurity': {
                'NSAllowsArbitraryLoads': True,
            },
            # Background app — no dock icon needed
            'LSUIElement': False,
            'LSBackgroundOnly': False,
        },
    )
