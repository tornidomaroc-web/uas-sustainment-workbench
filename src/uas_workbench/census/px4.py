"""Census probe for PX4 ULog files (.ulg), built on pyulog."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pyulog import ULog

from .model import (
    ABSENT,
    FieldResult,
    FieldSpec,
    FilledTest,
    FloatArray,
    LogCensus,
    equals,
    nonzero,
    positive,
    toggles,
)

ARMING_STATE_ARMED = 2  # VehicleStatus.msg
LOG_LEVEL_ERR = 3  # syslog levels used by PX4 logged messages; lower is more severe


def syslog_level(raw: int) -> int:
    """ULog stores the level as an ASCII digit ('3' == 51 is ERROR); accept raw 0-7 too."""
    return raw - ord("0") if raw >= ord("0") else raw


@dataclass(frozen=True)
class TopicField:
    spec: FieldSpec
    topic: str
    field_regex: str
    test: FilledTest


def _tf(
    key: str,
    counter: str,
    means: str,
    test: FilledTest,
    *,
    extra: bool = False,
    regex: str | None = None,
) -> TopicField:
    topic, _, fld = key.partition(".")
    return TopicField(FieldSpec(key, counter, means, extra), topic, regex or re.escape(fld), test)


TOPIC_FIELDS: tuple[TopicField, ...] = (
    _tf(
        "vehicle_status.arming_state",
        "arm_cycles",
        "ARMED (2) observed",
        equals(ARMING_STATE_ARMED),
    ),
    _tf("vehicle_status.armed_time", "arm_cycles", "non-zero timestamp", nonzero),
    _tf("vehicle_status.takeoff_time", "flight_time", "non-zero timestamp", nonzero),
    _tf("vehicle_land_detected.landed", "landings", "changes state (both values seen)", toggles),
    _tf("battery_status.discharged_mah", "battery_energy", "> 0 mAh", positive),
    _tf("battery_status.voltage_v", "battery_energy", "> 0 V", positive),
    _tf("battery_status.current_a", "battery_energy", "> 0 A", positive, extra=True),
    _tf("battery_status.cycle_count", "battery_energy", "> 0", positive),
    _tf("battery_status.state_of_health", "battery_energy", "> 0 %", positive),
    _tf("battery_status.serial_number", "battery_energy", "non-zero", nonzero, extra=True),
    _tf(
        "esc_status.esc[].esc_rpm",
        "esc_data",
        "any ESC reports non-zero rpm",
        nonzero,
        regex=r"esc\[\d+\]\.esc_rpm",
    ),
    _tf(
        "esc_status.esc[].esc_temperature",
        "esc_data",
        "any ESC reports non-zero temperature",
        nonzero,
        regex=r"esc\[\d+\]\.esc_temperature",
    ),
    _tf(
        "esc_status.esc[].esc_errorcount",
        "esc_data",
        "any ESC reports errors (> 0)",
        nonzero,
        regex=r"esc\[\d+\]\.esc_errorcount",
    ),
    _tf(
        "actuator_outputs.output[]",
        "esc_data",
        "any output non-zero (load proxy)",
        nonzero,
        regex=r"output\[\d+\]",
    ),
    _tf("failure_detector_status.fd_motor", "fault_events", "flag raised at least once", nonzero),
    _tf(
        "failure_detector_status.fd_imbalanced_prop",
        "fault_events",
        "flag raised at least once",
        nonzero,
    ),
    _tf("failure_detector_status.fd_battery", "fault_events", "flag raised at least once", nonzero),
    _tf("vehicle_gps_position.time_utc_usec", "utc_date", "non-zero (GPS time known)", nonzero),
    _tf("sensor_gps.time_utc_usec", "utc_date", "non-zero (GPS time known)", nonzero, extra=True),
)

PARAM_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("param LND_FLIGHT_T_HI", "lifetime_hours", "non-zero"),
    FieldSpec("param LND_FLIGHT_T_LO", "lifetime_hours", "non-zero"),
)
INFO_SYS_UUID = FieldSpec("info sys_uuid", "aircraft_key", "non-empty and not all zeros")
LOGGED_ERRORS = FieldSpec(
    "logged messages, level ERROR or worse", "fault_events", "at least one", extra=True
)

FIELD_SPECS: tuple[FieldSpec, ...] = (
    INFO_SYS_UUID,
    *(tf.spec for tf in TOPIC_FIELDS),
    *PARAM_FIELDS,
    LOGGED_ERRORS,
)
TOPICS: tuple[str, ...] = tuple(sorted({tf.topic for tf in TOPIC_FIELDS}))


def _topic_values(ulog: ULog, topic: str, field_regex: str) -> FloatArray | None:
    """All samples of every matching field across every instance of a topic, or None if the
    topic is not logged or its format has no such field."""
    pattern = re.compile(field_regex)
    chunks = [
        np.asarray(values, dtype=np.float64)
        for dataset in ulog.data_list
        if dataset.name == topic
        for name, values in dataset.data.items()
        if pattern.fullmatch(name)
    ]
    if not chunks:
        return None
    return np.concatenate(chunks)


def _probe_topic(ulog: ULog, tf: TopicField) -> FieldResult:
    values = _topic_values(ulog, tf.topic, tf.field_regex)
    if values is None or values.size == 0:
        return ABSENT
    return FieldResult(present=True, filled=tf.test(values))


def _probe_param(ulog: ULog, name: str) -> FieldResult:
    if name not in ulog.initial_parameters:
        return ABSENT
    return FieldResult(present=True, filled=float(ulog.initial_parameters[name]) != 0)


def _probe_uuid(ulog: ULog) -> FieldResult:
    value = ulog.msg_info_dict.get("sys_uuid")
    if value is None:
        return ABSENT
    text = str(value).strip().strip("\x00")
    return FieldResult(present=True, filled=bool(text) and set(text) != {"0"})


def _probe_logged_errors(ulog: ULog) -> FieldResult:
    messages = ulog.logged_messages
    if not messages:
        return ABSENT
    return FieldResult(
        present=True, filled=any(syslog_level(m.log_level) <= LOG_LEVEL_ERR for m in messages)
    )


def probe_ulog(path: Path, group: str) -> LogCensus:
    ulog = ULog(str(path), message_name_filter_list=list(TOPICS), disable_str_exceptions=True)

    fields: dict[str, FieldResult] = {INFO_SYS_UUID.key: _probe_uuid(ulog)}
    for tf in TOPIC_FIELDS:
        fields[tf.spec.key] = _probe_topic(ulog, tf)
    for spec in PARAM_FIELDS:
        fields[spec.key] = _probe_param(ulog, spec.key.removeprefix("param "))
    fields[LOGGED_ERRORS.key] = _probe_logged_errors(ulog)

    def filled(key: str) -> bool:
        return fields[key].filled

    landed_seen = _topic_values(ulog, "vehicle_land_detected", "landed")
    airborne = filled("vehicle_status.takeoff_time") or (
        landed_seen is not None and bool(np.any(landed_seen == 0))
    )
    counters = {
        "aircraft_key": filled(INFO_SYS_UUID.key),
        "utc_date": filled("vehicle_gps_position.time_utc_usec")
        or filled("sensor_gps.time_utc_usec"),
        "flight_time": filled("vehicle_land_detected.landed")
        or filled("vehicle_status.takeoff_time"),
        "arm_cycles": filled("vehicle_status.arming_state"),
        "landings": filled("vehicle_land_detected.landed"),
        "battery_energy": filled("battery_status.discharged_mah")
        or (filled("battery_status.voltage_v") and filled("battery_status.current_a")),
        "esc_data": filled("esc_status.esc[].esc_rpm"),
        # The detector publishes continuously, so its presence makes "no fault" a real answer.
        "fault_events": any(d.name == "failure_detector_status" for d in ulog.data_list),
        "lifetime_hours": filled("param LND_FLIGHT_T_HI") or filled("param LND_FLIGHT_T_LO"),
    }
    return LogCensus(
        source="px4",
        group=group,
        log_ref=path.stem,
        firmware=ulog.get_version_info_str() or "unknown",
        duration_s=(ulog.last_timestamp - ulog.start_timestamp) / 1e6,
        airborne=airborne,
        fields=fields,
        counters=counters,
    )
