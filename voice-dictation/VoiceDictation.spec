# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build: one file, no console window.

sounddevice ships the PortAudio DLL as package data, so it is collected
explicitly - PyInstaller does not find it on its own.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

datas = [("assets", "assets")]
datas += collect_data_files("sounddevice")

binaries = collect_dynamic_libs("sounddevice")

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
    "numpy",
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
