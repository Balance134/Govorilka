# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build: one file, no console window.

The PortAudio DLL does NOT live inside sounddevice: sounddevice ships as a
bare module (sounddevice.py), and the DLL sits in a separate top-level package
_sounddevice_data/portaudio-binaries/. Collecting "sounddevice" returns nothing
at all, so both calls point at "_sounddevice_data" instead.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# Ship the icons only - make_icons.py is a build-time script, not app data.
datas = [
    (str(item), "assets")
    for item in sorted(Path("assets").iterdir())
    if item.is_file() and item.suffix != ".py"
]
# The DLL itself comes in as a binary below, so it is not collected twice.
datas += collect_data_files("_sounddevice_data", excludes=["**/*.dll"])

binaries = collect_dynamic_libs("_sounddevice_data")

hiddenimports = [
    "sounddevice",
    "_sounddevice",
    "cffi",
    "_cffi_backend",
    "requests",
    "urllib3",
    "certifi",
    "idna",
    "charset_normalizer",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "winsound",
]

# Qt modules the app never touches; excluding them keeps the .exe smaller.
excludes = [
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtMultimedia",
    "PySide6.Qt3DCore",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "tkinter",
    "unittest",
    "pytest",
    "matplotlib",
]

a = Analysis(
    ["src/main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="VoiceDictation",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # no console window
    disable_windowed_traceback=False,
    icon="assets/icon.ico",
)
