from pathlib import Path

from pymavlink import DFReader

from builders.dataflash import DataFlashBuilder
from uas_workbench.census.excerpt import excerpt


def test_excerpt_keeps_counters_and_drops_location(tmp_path: Path) -> None:
    b = (
        DataFlashBuilder()
        .define("GPS", "QBIHiiffff", "TimeUS,Status,GMS,GWk,Lat,Lng,Alt,Spd,GCrs,VZ")
        .define("MSG", "QZ", "TimeUS,Message")
        .define("POS", "Qii", "TimeUS,Lat,Lng")
        .define("STAT", "QB", "TimeUS,isFlying")
    )
    b.msg("GPS", 1, 3, 1234, 2010, 123_456_789, -123_456_789, 310.5, 17.0, 90.0, -0.5)
    b.msg("MSG", 1, "PX4v2 DEADBEEF 00000000 00000001")
    b.msg("POS", 1, 123_456_789, -123_456_789)
    b.msg("STAT", 1, 1)
    src = b.write(tmp_path / "src.bin")

    out = tmp_path / "out.bin"
    out.write_bytes(excerpt(src.read_bytes()))
    reader = DFReader.DFReader_binary(str(out), zero_time_base=True)
    msgs = {m.get_type(): m for m in iter(reader.recv_match, None)}
    reader.close()

    assert set(msgs) == {"FMT", "GPS", "MSG", "STAT"}  # POS dropped entirely
    gps = msgs["GPS"]
    assert (gps.Lat, gps.Lng, gps.Alt, gps.GCrs) == (0, 0, 0.0, 0.0)
    assert (gps.GMS, gps.GWk, gps.Spd, gps.VZ) == (1234, 2010, 17.0, -0.5)  # untouched
    assert msgs["MSG"].Message == "PX4v2 XXXXXXXX XXXXXXXX XXXXXXXX"
    assert msgs["STAT"].isFlying == 1
