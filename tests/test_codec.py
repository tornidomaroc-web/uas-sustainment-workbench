import json
from datetime import UTC, datetime

from uas_workbench.flight import FaultEvent, FlightRecord, LifetimeCounter, Unknown
from uas_workbench.flight.codec import record_from_json, record_to_json


def test_record_round_trips_through_json_with_unknowns_explicit() -> None:
    record = FlightRecord(
        source="px4",
        log_ref="x",
        licence="CC0",
        attribution="synthetic",
        firmware="v1.15.2",
        log_span_s=100.5,
        aircraft_key="A",
        utc_start=datetime(2026, 8, 3, 9, 30, tzinfo=UTC),
        flight_time_s=80.25,
        arm_cycles=1,
        landings=Unknown("no vehicle_land_detected topic"),
        battery_mah=1200.0,
        battery_wh=Unknown("no current"),
        fault_events=(FaultEvent(12.5, "error_message", "Battery low"),),
        boot_count=Unknown("PX4 logs no boot counter"),
        lifetime=LifetimeCounter(10.0, 10.0, 0.0),
        synthetic=True,
    )
    data = record_to_json(record)
    assert data["landings"] == {"unknown": "no vehicle_land_detected topic"}
    assert data["utc_start"] == "2026-08-03T09:30:00+00:00"
    assert data["fault_events"] == [{"t_s": 12.5, "kind": "error_message", "detail": "Battery low"}]
    assert data["synthetic"] is True
    assert record_from_json(json.loads(json.dumps(data))) == record
