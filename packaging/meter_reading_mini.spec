from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH).parent
package = root / "src" / "meter_reading_mini"

datas = [
    (str(package / "db" / name), str(Path("meter_reading_mini") / "db"))
    for name in ("schema.sql", "schema_v1.sql", "schema_v3.sql")
]
datas += [
    (
        str(package / "core" / "mobile_reading_assets" / name),
        str(Path("meter_reading_mini") / "core" / "mobile_reading_assets"),
    )
    for name in ("sheet.html", "sheet.css", "sheet.js")
]
datas += collect_data_files("tzdata")

analysis = Analysis(
    [str(package / "__main__.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("tzdata.zoneinfo"),
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="meter-reading-mini",
    console=False,
    upx=False,
)
coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="meter-reading-mini",
)
