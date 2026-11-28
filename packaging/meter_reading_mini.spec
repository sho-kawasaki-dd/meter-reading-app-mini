from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
package = root / "src" / "meter_reading_mini"
common_package = root / "src" / "building_manager_mini"
entry_point = root / "src" / "building_manager_mini" / "__main__.py"

datas = [
    (
        str(common_package / "db" / "schema.sql"),
        str(Path("building_manager_mini") / "db"),
    ),
    (
        str(package / "assets" / "meter-reading-mini.ico"),
        str(Path("meter_reading_mini") / "assets"),
    ),
    *[
        (str(package / "db" / name), str(Path("meter_reading_mini") / "db"))
        for name in (
            "schema.sql",
            "schema_v1.sql",
            "schema_v3.sql",
            "schema_v4.sql",
            "schema_v5.sql",
        )
    ],
]
datas += [
    (
        str(package / "core" / "mobile_reading_assets" / name),
        str(Path("meter_reading_mini") / "core" / "mobile_reading_assets"),
    )
    for name in ("sheet.html", "sheet.css", "sheet.js")
]
datas += collect_data_files("tzdata")
application_metadata = copy_metadata("building-manager-mini")
if len(application_metadata) != 1:
    raise RuntimeError("building-manager-mini の配布メタデータを一つに特定できません。")
application_metadata_source, application_metadata_destination = application_metadata[0]
application_entry_points = Path(application_metadata_source) / "entry_points.txt"
if not application_entry_points.is_file():
    raise RuntimeError(f"アプリの entry points がありません: {application_entry_points}")
datas.append((str(application_entry_points), application_metadata_destination))

analysis = Analysis(
    [str(entry_point)],
    pathex=[str(root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "building_manager_mini.core.settings_service",
        "building_manager_mini.db.migrations",
        "building_manager_mini.db.settings_repository",
        "meter_reading_mini.app",
        *collect_submodules("tzdata.zoneinfo"),
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

excluded_names = {
    "qt6pdf.dll",
    "qt6qml.dll",
    "qt6qmlmeta.dll",
    "qt6qmlmodels.dll",
    "qt6qmlworkerscript.dll",
    "qt6quick.dll",
    "qt6virtualkeyboard.dll",
    "qt6network.dll",
    "qtnetwork.pyd",
    "opengl32sw.dll",
    "qpdf.dll",
    "qicns.dll",
    "qtga.dll",
    "qtiff.dll",
    "qwbmp.dll",
    "qwebp.dll",
    "qdirect2d.dll",
    "qminimal.dll",
    "qoffscreen.dll",
}
excluded_directories = (
    "pyside6/translations/",
    "pyside6/plugins/tls/",
    "pyside6/plugins/networkinformation/",
    "pyside6/plugins/generic/",
    "pyside6/plugins/platforminputcontexts/",
)


def included(entry):
    destination = str(entry[0]).replace("\\", "/").lower()
    return (
        destination.rsplit("/", 1)[-1] not in excluded_names
        and not destination.startswith(excluded_directories)
    )


analysis.binaries = [entry for entry in analysis.binaries if included(entry)]
analysis.datas = [entry for entry in analysis.datas if included(entry)]
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    icon=str(package / "assets" / "meter-reading-mini.ico"),
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
