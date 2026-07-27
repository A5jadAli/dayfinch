# -*- mode: python ; coding: utf-8 -*-

import importlib.util
import sys

from PyInstaller.utils.hooks import collect_submodules


hidden_imports = ["mss", "pynput.keyboard", "pynput.mouse", "pystray"]
if sys.platform == "win32":
    hidden_imports.extend(
        ["mss.windows", "pynput.keyboard._win32", "pynput.mouse._win32", "pystray._win32"]
    )
elif sys.platform == "darwin":
    hidden_imports.extend(
        ["mss.darwin", "pynput.keyboard._darwin", "pynput.mouse._darwin", "pystray._darwin"]
    )
else:
    hidden_imports.extend(
        ["mss.linux", "pynput.keyboard._xorg", "pynput.mouse._xorg", "pystray._xorg"]
    )
if importlib.util.find_spec("dbus_next") is not None:
    hidden_imports.extend(collect_submodules("dbus_next"))

a = Analysis(
    ["agent_entry.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Dayfinch-Agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
