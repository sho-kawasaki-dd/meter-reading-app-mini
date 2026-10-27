from __future__ import annotations

import ast
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

CORE_ROOT = Path(__file__).resolve().parents[1] / "src" / "meter_reading_mini" / "core"
CONTRACT_FILES = ("contract_models.py", "meter_reading_sync_repositories.py")
FORBIDDEN_IMPORTS = ("api", "application", "db", "ui", "core.database")


def test_contract_modules_do_not_import_services_ui_or_database() -> None:
    for filename in CONTRACT_FILES:
        tree = ast.parse((CORE_ROOT / filename).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported_modules = [node.module or ""]
            else:
                continue

            for module in imported_modules:
                assert not any(
                    module == forbidden or module.startswith(f"{forbidden}.")
                    for forbidden in FORBIDDEN_IMPORTS
                )
                if module.startswith("meter_reading_mini.core."):
                    assert module == "meter_reading_mini.core.contract_models"


def test_measurement_definition_update_contract_omits_immutable_fields() -> None:
    update_fields = {field.name for field in fields(MeasurementDefinitionUpdateRequest)}

    assert (
        not {
            "reading_cycle",
            "value_schema",
            "calculation_method",
            "location_id",
        }
        & update_fields
    )


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
