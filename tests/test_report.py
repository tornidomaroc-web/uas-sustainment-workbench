from uas_workbench.census import report
from uas_workbench.census.model import COUNTERS, FieldResult, FieldSpec, LogCensus

SPECS = (FieldSpec("battery_status.discharged_mah", "battery_energy", "> 0 mAh"),)


def census(
    ref: str, airborne: bool, filled: bool, firmware: str = "v1.15.2", error: str | None = None
) -> LogCensus:
    return LogCensus(
        source="px4",
        group="fixed_wing",
        log_ref=ref,
        firmware=firmware,
        duration_s=300.0,
        airborne=airborne,
        error=error,
        fields={"battery_status.discharged_mah": FieldResult(True, filled)},
        counters=dict.fromkeys(COUNTERS, filled),
    )


def test_counts_and_airborne_denominator() -> None:
    results = [
        census("a", airborne=True, filled=True),
        census("b", airborne=True, filled=False, firmware="v1.14.3"),
        census("c", airborne=False, filled=True),
        census("d", airborne=False, filled=False, error="ValueError: corrupt"),
    ]
    s = report.summarise("px4", "fixed_wing", results, SPECS)

    assert (s.logs, s.unreadable, s.airborne) == (4, 1, 2)
    assert s.firmware == {"v1.14": 1, "v1.15": 2}
    f = s.fields[0]
    assert (f.present, f.filled, f.filled_airborne) == (3, 2, 1)
    esc = next(c for c in s.counters if c.name == "esc_data")
    assert (esc.derivable, esc.derivable_airborne) == (2, 1)


def test_markdown_shows_fractions() -> None:
    s = report.summarise("px4", "fixed_wing", [census("a", True, True)], SPECS)
    text = report.to_markdown([s])
    assert "| battery_status.discharged_mah | battery_energy | > 0 mAh | 1/1 (100 %)" in text
    assert "| esc_data (needs a flight) | 1/1 (100 %) | 1/1 (100 %) |" in text


def test_firmware_family() -> None:
    assert report._firmware_family("ArduPlane 3.9.2") == "ArduPlane 3.9"
    assert report._firmware_family("v1.14.3") == "v1.14"
    assert report._firmware_family("unknown") == "unknown"
