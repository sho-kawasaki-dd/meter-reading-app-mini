"""MeterMasterService の単体テスト。"""

from __future__ import annotations

from application.meter_master_service import (
    METER_CATEGORY_VALUES,
    MeterCreateRequest,
    MeterMasterService,
    MeterNotFoundError,
    MeterUpdateRequest,
    MeterValidationError,
)
from core.database import PhysicalMeterORM


def test_meter_master_service_lists_active_and_inactive_meters(session_factory) -> None:
    with session_factory() as session:
        session.add(
            PhysicalMeterORM(
                meter_id="meter-active",
                location_id="loc-001",
                location_name="Location 1",
                meter_name="Active Meter",
                category=METER_CATEGORY_VALUES[0],
                meter_serial=None,
                is_active=True,
            )
        )
        session.add(
            PhysicalMeterORM(
                meter_id="meter-inactive",
                location_id="loc-002",
                location_name="Location 2",
                meter_name="Inactive Meter",
                category=METER_CATEGORY_VALUES[1],
                meter_serial=None,
                is_active=False,
            )
        )
        session.commit()

        service = MeterMasterService(session)
        assert [meter.meter_id for meter in service.list_meters(show_inactive=False)] == [
            "meter-active"
        ]
        assert [meter.meter_id for meter in service.list_meters(show_inactive=True)] == [
            "meter-active",
            "meter-inactive",
        ]


def test_meter_master_service_create_update_and_delete_meter(session_factory) -> None:
    with session_factory() as session:
        service = MeterMasterService(session)
        created = service.create_meter(
            MeterCreateRequest(
                meter_id="meter-new",
                location_id="loc-new",
                location_name="New Location",
                meter_name="New Meter",
                category=METER_CATEGORY_VALUES[0],
                meter_serial="SER-NEW",
            )
        )
        session.commit()

        stored = session.get(PhysicalMeterORM, "meter-new")
        assert stored is not None
        assert stored.meter_name == "New Meter"
        assert created.meter_id == stored.meter_id

        updated = service.update_meter(
            "meter-new",
            MeterUpdateRequest(
                location_id="loc-updated",
                location_name="Updated Location",
                meter_name="Updated Meter",
                category=METER_CATEGORY_VALUES[1],
                meter_serial=None,
            ),
        )
        session.commit()

        assert updated.location_name == "Updated Location"
        assert session.get(PhysicalMeterORM, "meter-new").category == METER_CATEGORY_VALUES[1]

        service.delete_meter("meter-new")
        session.commit()

        assert session.get(PhysicalMeterORM, "meter-new").is_active is False


def test_meter_master_service_validates_and_rejects_missing_or_duplicate_meter_id(
    session_factory,
) -> None:
    with session_factory() as session:
        service = MeterMasterService(session)
        service.create_meter(
            MeterCreateRequest(
                meter_id="meter-dup",
                location_id="loc-001",
                location_name="Location 1",
                meter_name="Meter 1",
                category=METER_CATEGORY_VALUES[0],
            )
        )
        session.commit()

        try:
            service.create_meter(
                MeterCreateRequest(
                    meter_id="meter-dup",
                    location_id="loc-002",
                    location_name="Location 2",
                    meter_name="Meter 2",
                    category=METER_CATEGORY_VALUES[1],
                )
            )
        except MeterValidationError as exc:
            assert "既に存在" in exc.errors[0]
        else:
            raise AssertionError("Expected duplicate meter_id validation error")

        try:
            service.update_meter(
                "missing-meter",
                MeterUpdateRequest(
                    location_id="loc-001",
                    location_name="Location 1",
                    meter_name="Meter 1",
                    category=METER_CATEGORY_VALUES[0],
                ),
            )
        except MeterNotFoundError as exc:
            assert exc.meter_id == "missing-meter"
        else:
            raise AssertionError("Expected missing meter error")
