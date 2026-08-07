# -*- mode: python ; coding: utf-8 -*-
"""
c69update.spec — Build c69update.exe (standalone updater with tkinter UI).

Generic helper — không đổi giữa các lần build trừ khi sửa c69update.py, nhưng vẫn build
lại mỗi lần "build_router.py" chạy để đảm bảo luôn khớp phiên bản Python/psutil hiện tại.
"""

a = Analysis(
    ['c69update.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'psutil',
        'tkinter',
        'tkinter.ttk',
        '_tkinter',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=2,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='c69update',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,       # Ẩn console, chỉ hiện UI window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
