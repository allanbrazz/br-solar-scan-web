from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


hiddenimports = (
    collect_submodules("config")
    + collect_submodules("core")
    + collect_submodules("whitenoise")
)
datas = [
    ("templates", "templates"),
    ("static", "static"),
    ("staticfiles", "staticfiles"),
]
datas += collect_data_files("django")
datas += copy_metadata("Django")

a = Analysis(
    ["run_app.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "gunicorn",
        "psycopg",
        "psycopg_binary",
        "tensorflow",
        "tensorboard",
        "keras",
        "torch",
        "torchgen",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BrazSolarScan",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="BrazSolarScan",
)
