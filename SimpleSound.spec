# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

datas = [('core', 'core'), ('ui', 'ui'), ('assets', 'assets'), ('styles', 'styles')]
datas += collect_data_files('sounddevice')


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'PySide6.QtSvg', 'PySide6.QtNetwork', 'numpy', 'pydub', 'pydub.effects', 'sounddevice', '_sounddevice_data', 'core.models', 'core.audio_engine', 'core.theme', 'core.waveform_cache', 'ui.main_window', 'ui.canvas', 'ui.widgets', 'ui.loaders', 'ui.help_dialog', 'ui.export_dialog'],
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
    name='SimpleSound',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\icons\\app.ico'],
)
