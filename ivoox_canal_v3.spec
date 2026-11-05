# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

# curl_cffi lleva una librería nativa (libcurl-impersonate) que hay que empaquetar
cffi_datas, cffi_binaries, cffi_hidden = collect_all('curl_cffi')


a = Analysis(
    ['ivoox_canal_v3.py'],
    pathex=[],
    binaries=cffi_binaries,
    datas=cffi_datas,
    hiddenimports=cffi_hidden,
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
    name='ivoox_canal_v3',
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
)
