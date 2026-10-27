from __future__ import annotations

import ast
import re
from dataclasses import fields
from pathlib import Path

from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionUpdateRequest,
    PlannedReadingCloseReason,
    ReplacementBoundaryRecord,
    ResultSource,
    UpsertDisposition,
    UpsertOutcome,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "meter_reading_mini"
CORE_ROOT = PACKAGE_ROOT / "core"
DB_ROOT = PACKAGE_ROOT / "db"
CONTRACT_FILES = ("contract_models.py", "errors.py", "meter_reading_sync_repositories.py")
CONTRACT_MODULES = {
    "meter_reading_mini.core.contract_models",
    "meter_reading_mini.core.errors",
    "meter_reading_mini.core.meter_reading_sync_repositories",
}
DATABASE_CORE_IMPORTS = CONTRACT_MODULES | {"meter_reading_mini.core.timeutil"}
LOCALTIME_LITERAL = re.compile(r"""(?i)(['"])localtime\1""")


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _sql_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.sql"))


def _imported_modules(tree: ast.AST, path: Path) -> list[str]:
    modules: list[str] = []
    source_module = ("meter_reading_mini", *path.relative_to(PACKAGE_ROOT).with_suffix("").parts)
    package_parts = source_module[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                module = node.module or ""
                modules.append(module)
                if module == "meter_reading_mini":
                    modules.extend(f"{module}.{alias.name}" for alias in node.names)
            else:
                base_parts = package_parts[: len(package_parts) - node.level + 1]
                if node.module:
                    modules.append(".".join((*base_parts, *node.module.split("."))))
                else:
                    modules.extend(".".join((*base_parts, alias.name)) for alias in node.names)
    return modules


def _is_module_or_submodule(module: str, root: str) -> bool:
    return module == root or module.startswith(f"{root}.")


def _attribute_chain(node: ast.expr) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (*_attribute_chain(node.value), node.attr)
    return ()


def _uses_direct_wall_clock(tree: ast.AST) -> bool:
    direct_imports: dict[str, str] = {}
    module_imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "datetime":
            for alias in node.names:
                if alias.name in {"date", "datetime"}:
                    direct_imports[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "datetime":
                    module_imports[alias.asname or alias.name] = alias.name

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        chain = _attribute_chain(node.func)
        if not chain:
            continue
        if chain[0] in direct_imports:
            normalized = (direct_imports[chain[0]], *chain[1:])
        elif chain[0] in module_imports:
            normalized = (module_imports[chain[0]], *chain[1:])
        else:
            continue
        if normalized[-2:] in {("datetime", "now"), ("date", "today")}:
            return True
    return False


def test_contract_modules_do_not_import_services_ui_or_database() -> None:
    for filename in CONTRACT_FILES:
        path = CORE_ROOT / filename
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for module in _imported_modules(tree, path):
            assert not _is_module_or_submodule(module, "api")
            assert not _is_module_or_submodule(module, "application")
            assert not _is_module_or_submodule(module, "core")
            assert not _is_module_or_submodule(module, "core.database")
            assert not _is_module_or_submodule(module, "db")
            assert not _is_module_or_submodule(module, "ui")
            assert not _is_module_or_submodule(module, "meter_reading_mini.db")
            assert not _is_module_or_submodule(module, "meter_reading_mini.ui")
            if _is_module_or_submodule(module, "meter_reading_mini.core"):
                assert module in CONTRACT_MODULES


def test_core_and_db_do_not_import_pyside6() -> None:
    for path in (*_python_files(CORE_ROOT), *_python_files(DB_ROOT)):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not any(
            _is_module_or_submodule(module, "PySide6") for module in _imported_modules(tree, path)
        ), path


def test_database_does_not_import_services_or_ui() -> None:
    for path in _python_files(DB_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for module in _imported_modules(tree, path):
            assert not _is_module_or_submodule(module, "ui")
            assert not _is_module_or_submodule(module, "meter_reading_mini.ui")
            assert not _is_module_or_submodule(module, "api")
            assert not _is_module_or_submodule(module, "application")
            assert not _is_module_or_submodule(module, "core")
            assert not _is_module_or_submodule(module, "core.database")
            if _is_module_or_submodule(module, "meter_reading_mini.core"):
                assert module in DATABASE_CORE_IMPORTS


def test_direct_wall_clock_access_is_limited_to_timeutil() -> None:
    allowed_path = CORE_ROOT / "timeutil.py"
    for path in (*_python_files(CORE_ROOT), *_python_files(DB_ROOT)):
        if path == allowed_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not _uses_direct_wall_clock(tree), path


def test_sql_does_not_use_localtime_modifier() -> None:
    for path in (*_python_files(CORE_ROOT), *_python_files(DB_ROOT), *_sql_files(DB_ROOT)):
        assert LOCALTIME_LITERAL.search(path.read_text(encoding="utf-8")) is None, path


def test_measurement_definition_update_contract_exposes_immutable_fields_for_rejection() -> None:
    update_fields = {field.name for field in fields(MeasurementDefinitionUpdateRequest)}

    assert {
        "reading_cycle",
        "value_schema",
        "calculation_method",
        "location_id",
    } <= update_fields


def test_contract_vocabularies_cover_replacement_and_duplicate_outcomes() -> None:
    assert {reason.value for reason in PlannedReadingCloseReason} == {
        "cutoff_elapsed",
        "meter_replaced",
        "schedule_recomposed",
        "definition_deactivated",
    }
    assert {source.value for source in ResultSource} == {"mobile_html", "manual"}
    assert UpsertDisposition.IGNORED_DUPLICATE.value == "ignored_duplicate"
    assert UpsertOutcome(UpsertDisposition.IGNORED_DUPLICATE).accepted is False


def test_replacement_boundary_contract_keeps_missing_initial_reading_explicit() -> None:
    boundary = ReplacementBoundaryRecord(
        replacement_event_id="replacement-001",
        measurement_definition_id="definition-001",
        new_initial_reading=None,
        new_meter_id="meter-002",
    )

    assert boundary.new_initial_reading is None
    assert boundary.new_meter_id == "meter-002"
