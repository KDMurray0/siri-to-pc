# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the Music Request desktop client.

The client is only a window onto a server, so it carries none of the server's
dependencies -- just pywebview and the .NET bridge it needs for WebView2.

Build:  python -m PyInstaller --noconfirm MusicClient.spec
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

binaries = []
datas = []
hiddenimports = ['clr'] + collect_submodules('webview')
for _pkg in ('pythonnet', 'clr_loader'):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

_EXCLUDES = [
    'torch', 'torchvision', 'torchaudio', 'tensorflow', 'scipy', 'pandas',
    'matplotlib', 'sklearn', 'transformers', 'sympy', 'cv2', 'numpy', 'IPython',
    'notebook', 'jupyter', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'tkinter',
    'test', 'mrs', 'fastapi', 'uvicorn', 'starlette', 'PIL',
]

a = Analysis(
    ['client.pyw'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MusicClient',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon='MusicClient.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='MusicClient',
)
