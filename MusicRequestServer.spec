# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for Music Request Server.

Builds a windowed MusicRequestServer.exe that runs the FastAPI server
in-process and shows the pywebview tray flyout. mpv, yt-dlp and node are NOT
bundled — they must be on PATH at runtime. Config and state live in
%LOCALAPPDATA%\MusicRequestServer (see mrs/paths.py).

Build:  pyinstaller --noconfirm MusicRequestServer.spec
"""

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

# Templates are loaded via mrs.paths.resource_dir() -> _MEIPASS/web/templates
datas = [('src/mrs/web/templates', 'web/templates')]
binaries = []
hiddenimports = [
    'clr', 'pystray._win32',
    # uvicorn resolves these by string at runtime, so PyInstaller can't see them
    'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.loops.asyncio',
    'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto',
    'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan', 'uvicorn.lifespan.on',
]
hiddenimports += collect_submodules('mrs')

# ytmusicapi ships locale/oauth JSON it loads at runtime.
datas += collect_data_files('ytmusicapi')
hiddenimports += collect_submodules('webview')
for _pkg in ('fastapi', 'starlette', 'uvicorn', 'jinja2'):
    try:
        hiddenimports += collect_submodules(_pkg)
    except Exception:
        pass

# pythonnet (clr) powers pywebview's WebView2 backend; edge_tts/certifi give the
# neural announce voice. Pull their data + native DLLs in explicitly.
for _pkg in ('pythonnet', 'clr_loader', 'edge_tts', 'certifi'):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:
        pass


# Nothing here uses the scientific/ML stack; exclude it so the build stays lean
# (the dev Python has torch/scipy installed, which PyInstaller would otherwise drag in).
_EXCLUDES = [
    'torch', 'torchvision', 'torchaudio', 'tensorflow', 'scipy', 'pandas',
    'matplotlib', 'sklearn', 'scikit-learn', 'transformers', 'sympy', 'cv2',
    'numpy', 'IPython', 'notebook', 'jupyter', 'jupyterlab', 'nbconvert',
    'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'tkinter', 'test',
]

a = Analysis(
    ['launcher.pyw'],
    pathex=['src'],
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

# One-dir build: most reliable for the pythonnet/pywebview (.NET) stack.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MusicRequestServer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='MusicRequestServer',
)
