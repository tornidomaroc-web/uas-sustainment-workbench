"""Unit tests for reconcile() on hand-built records."""

from datetime import UTC, datetime, timedelta

from uas_workbench.flight import FlightRecord, LifetimeCounter, Maybe, Unknown, reconcile
from uas_workbench.flight.record import Source

T0 = datetime(2024, 5, 1, 9, 0, tzinfo=UTC)
NO_BOOT = Unknown("no boot counter")
NO_FIX = Unknown("no fix")


def rec(
    ref: str,
    *,
    at_boot: float,
    flight: Maybe[float],
    last_seen: float | None = None,
    last_at: float = 0.0,
    boot: Maybe[int] = NO_BOOT,
    utc: Maybe[datetime] = NO_FIX,
    key: Maybe[str] = "A",
    span: float = 600.0,
    source: Source = "px4",
) -> FlightRecord:
    return FlightRecord(
        source=source,
        log_ref=ref,
        licence="CC0",
        attribution="synthetic",
        firmware="test",
        log_span_s=span,
        aircraft_key=key,
        utc_start=utc,
        flight_time_s=flight,
        arm_cycles=1,
        landings=1,
        battery_mah=Unknown("n/a"),
        battery_wh=Unknown("n/a"),
        fault_events=(),
        boot_count=boot,
        lifetime=LifetimeCounter(at_boot, at_boot if last_seen is None else last_seen, last_at),
    )


def test_px4_style_logs_are_ordered_by_utc_and_agree() -> None:
    logs = [
        rec("b", at_boot=4000.0, flight=1000.0, utc=T0 + timedelta(hours=2)),
        rec("a", at_boot=3000.0, flight=999.4, utc=T0),
    ]
    result = reconcile(logs)
    assert [(c.log_ref, c.next_log_ref, round(c.unlogged_s, 1)) for c in result.coverage] == [
        ("a", "b", 0.6)
    ]
    assert result.coverage[0].boots_between == Unknown("no boot counter")
    assert result.findings == ()
    assert result.unchecked == {
        "b": "last known log of this aircraft; the counter was not seen again"
    }


def test_duplicate_upload_is_named_not_counted() -> None:
    logs = [
        rec("a", at_boot=3000.0, flight=1000.0, utc=T0),
        rec("a-again", at_boot=3000.0, flight=1000.0, utc=T0 + timedelta(seconds=0.2)),
        rec("b", at_boot=4000.0, flight=500.0, utc=T0 + timedelta(hours=2)),
    ]
    result = reconcile(logs)
    assert [c.log_ref for c in result.coverage] == ["a"]
    assert result.unchecked["a-again"] == "duplicate of a"
    assert result.findings == ()


def test_boot_gap_and_unlogged_flight() -> None:
    logs = [
        rec("a", at_boot=100.0, flight=200.0, boot=5, source="ardupilot"),
        rec("b", at_boot=900.0, flight=50.0, boot=9, source="ardupilot"),
    ]
    result = reconcile(logs)
    assert result.coverage[0].boots_between == 3
    (finding,) = result.findings
    assert (finding.kind, finding.seconds) == ("unlogged_flight", 600.0)
    assert finding.message == (
        "600 s of flight on aircraft A is not covered by any log (after a, before b)"
    )


def test_counter_that_lags_the_log_is_a_mismatch_not_a_gap() -> None:
    logs = [
        rec("a", at_boot=100.0, flight=500.0, last_seen=150.0, last_at=480.0, boot=1),
        rec("b", at_boot=160.0, flight=10.0, boot=2),
    ]
    result = reconcile(logs)
    kinds = sorted((f.kind, f.log_ref) for f in result.findings)
    assert kinds == [("counter_mismatch", "a"), ("counter_mismatch", "a")]
    assert {round(f.seconds) for f in result.findings} == {-440, -450}


def test_records_that_cannot_be_checked_say_why() -> None:
    logs = [
        rec("nokey", at_boot=1.0, flight=1.0, key=Unknown("board id masked")),
        rec("noflight", at_boot=1.0, flight=Unknown("no STAT messages")),
        rec("noorder", at_boot=1.0, flight=1.0),
        rec("noorder2", at_boot=2.0, flight=1.0),
    ]
    result = reconcile(logs)
    assert result.unchecked == {
        "nokey": "aircraft unknown: board id masked",
        "noflight": "no flight time: no STAT messages",
        "noorder": "cannot order the logs of this aircraft: boot count and UTC start unknown",
        "noorder2": "cannot order the logs of this aircraft: boot count and UTC start unknown",
    }
    assert result.coverage == ()


def test_explicit_aircraft_key_overrides_unknown_keys() -> None:
    logs = [
        rec("a", at_boot=0.0, flight=100.0, boot=1, key=Unknown("masked")),
        rec("b", at_boot=100.0, flight=100.0, boot=2, key=Unknown("masked")),
    ]
    result = reconcile(logs, aircraft_key="fleet-03")
    assert result.coverage[0].aircraft_key == "fleet-03"
    assert result.findings == ()
