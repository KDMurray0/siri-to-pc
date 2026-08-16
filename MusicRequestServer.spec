# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for Music Request Server.

Builds a single windowed MusicRequestServer.exe that runs the Flask server
in-process and shows the pywebview tray flyout. External tools mpv, yt-dlp and
node are NOT bundled — they must be on PATH at runtime. config.json and the
JSON state files live next to the .exe (see src/paths.py).

Build:  pyinstaller --noconfirm MusicRequestServer.spec
"""

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

datas = [('src/templates', 'templates')]
binaries = []
hiddenimports = [
    'app', 'player', 'search', 'matching', 'auth', 'paths',
    'clr', 'pystray._win32',
]

# ytmusicapi ships locale/oauth JSON it loads at runtime.
datas += collect_data_files('ytmusicapi')
hiddenimports += collect_submodules('webview')

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
