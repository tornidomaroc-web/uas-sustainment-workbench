### px4 / whole index (469733 public logs; same filters as the sample)

| Group | Eligible logs | Aircraft key (sys_uuid) filled | Distinct aircraft | Logs with ERROR-level messages |
|---|---|---|---|---|
| fixed_wing | 13041 | 12989/13041 (99.6 %) | 1932 | 4170/13041 (32 %) |
| vtol | 18509 | 18490/18509 (99.9 %) | 2010 | 4847/18509 (26 %) |

### ardupilot / alfa_fixed_wing

10 logs, 0 unreadable, 9 airborne; median length 921 s. Firmware: ArduPlane 3.9: 10.

| Field | Serves | 'Real value' means | Logs with field | Logs with real values | Real values, airborne logs |
|---|---|---|---|---|---|
| MSG boot banner with MCU id *(extra)* | aircraft_key | id pattern found | 10/10 (100 %) | 10/10 (100 %) | 9/9 (100 %) |
| ARM.ArmState | arm_cycles | armed (1) observed | 0/10 (0 %) | 0/10 (0 %) | 0/9 (0 %) |
| EV.Id | arm_cycles | ARMED event (10) observed | 0/10 (0 %) | 0/10 (0 %) | 0/9 (0 %) |
| STAT.isFlying | landings | changes state (both values seen) | 10/10 (100 %) | 9/10 (90 %) | 9/9 (100 %) |
| STAT.Armed | arm_cycles | armed observed | 10/10 (100 %) | 10/10 (100 %) | 9/9 (100 %) |
| STAT.Crash | fault_events | crash flagged | 10/10 (100 %) | 0/10 (0 %) | 0/9 (0 %) |
| STAT.Hit | fault_events | hit flagged | 10/10 (100 %) | 0/10 (0 %) | 0/9 (0 %) |
| BAT.CurrTot (CURR before 3.10) | battery_energy | > 0 mAh | 0/10 (0 %) | 0/10 (0 %) | 0/9 (0 %) |
| BAT.EnrgTot (CURR before 3.10) | battery_energy | > 0 Wh | 0/10 (0 %) | 0/10 (0 %) | 0/9 (0 %) |
| BAT.SH | battery_energy | non-zero | 0/10 (0 %) | 0/10 (0 %) | 0/9 (0 %) |
| ESC.RPM | esc_data | non-zero rpm | 0/10 (0 %) | 0/10 (0 %) | 0/9 (0 %) |
| ESC.Temp | esc_data | non-zero temperature | 0/10 (0 %) | 0/10 (0 %) | 0/9 (0 %) |
| ESC.Err | esc_data | errors reported (> 0) | 0/10 (0 %) | 0/10 (0 %) | 0/9 (0 %) |
| ERR.ECode | fault_events | non-zero error code | 0/10 (0 %) | 0/10 (0 %) | 0/9 (0 %) |
| GPS.GWk | utc_date | non-zero GPS week | 10/10 (100 %) | 10/10 (100 %) | 9/9 (100 %) |
| RCOU.C1..C14 | esc_data | any output non-zero (load proxy) | 10/10 (100 %) | 10/10 (100 %) | 9/9 (100 %) |
| PARM STAT_FLTTIME | lifetime_hours | non-zero | 10/10 (100 %) | 10/10 (100 %) | 9/9 (100 %) |
| PARM STAT_BOOTCNT | lifetime_hours | non-zero | 10/10 (100 %) | 10/10 (100 %) | 9/9 (100 %) |
| PARM STAT_RUNTIME | lifetime_hours | non-zero | 10/10 (100 %) | 10/10 (100 %) | 9/9 (100 %) |
| PARM STAT_FLTCNT | landings | non-zero | 0/10 (0 %) | 0/10 (0 %) | 0/9 (0 %) |
| PARM BRD_SERIAL_NUM *(extra)* | aircraft_key | non-zero (user-set) | 10/10 (100 %) | 0/10 (0 %) | 0/9 (0 %) |
| PARM BATT_SERIAL_NUM *(extra)* | battery_energy | not 0 or -1 | 0/10 (0 %) | 0/10 (0 %) | 0/9 (0 %) |

| Counter | Derivable, all logs | Derivable, airborne logs |
|---|---|---|
| aircraft_key | 10/10 (100 %) | 9/9 (100 %) |
| utc_date | 10/10 (100 %) | 9/9 (100 %) |
| flight_time (needs a flight) | 9/10 (90 %) | 9/9 (100 %) |
| arm_cycles | 0/10 (0 %) | 0/9 (0 %) |
| landings (needs a flight) | 9/10 (90 %) | 9/9 (100 %) |
| battery_energy (needs a flight) | 0/10 (0 %) | 0/9 (0 %) |
| esc_data (needs a flight) | 0/10 (0 %) | 0/9 (0 %) |
| fault_events | 0/10 (0 %) | 0/9 (0 %) |
| lifetime_hours | 10/10 (100 %) | 9/9 (100 %) |

### px4 / fixed_wing

20 logs, 0 unreadable, 18 airborne; median length 430 s. Firmware: v0.0: 1, v1.11: 2, v1.13: 2, v1.14: 6, v1.15: 4, v1.16: 2, v1.17: 1, v1.9: 1, v180.6: 1.

| Field | Serves | 'Real value' means | Logs with field | Logs with real values | Real values, airborne logs |
|---|---|---|---|---|---|
| info sys_uuid | aircraft_key | non-empty and not all zeros | 20/20 (100 %) | 20/20 (100 %) | 18/18 (100 %) |
| vehicle_status.arming_state | arm_cycles | ARMED (2) observed | 20/20 (100 %) | 18/20 (90 %) | 18/18 (100 %) |
| vehicle_status.armed_time | arm_cycles | non-zero timestamp | 16/20 (80 %) | 14/20 (70 %) | 14/18 (78 %) |
| vehicle_status.takeoff_time | flight_time | non-zero timestamp | 16/20 (80 %) | 13/20 (65 %) | 13/18 (72 %) |
| vehicle_land_detected.landed | landings | changes state (both values seen) | 20/20 (100 %) | 18/20 (90 %) | 18/18 (100 %) |
| battery_status.discharged_mah | battery_energy | > 0 mAh | 19/20 (95 %) | 17/20 (85 %) | 16/18 (89 %) |
| battery_status.voltage_v | battery_energy | > 0 V | 19/20 (95 %) | 19/20 (95 %) | 18/18 (100 %) |
| battery_status.current_a *(extra)* | battery_energy | > 0 A | 19/20 (95 %) | 18/20 (90 %) | 17/18 (94 %) |
| battery_status.cycle_count | battery_energy | > 0 | 19/20 (95 %) | 0/20 (0 %) | 0/18 (0 %) |
| battery_status.state_of_health | battery_energy | > 0 % | 17/20 (85 %) | 0/20 (0 %) | 0/18 (0 %) |
| battery_status.serial_number *(extra)* | battery_energy | non-zero | 18/20 (90 %) | 0/20 (0 %) | 0/18 (0 %) |
| esc_status.esc[].esc_rpm | esc_data | any ESC reports non-zero rpm | 1/20 (5 %) | 1/20 (5 %) | 1/18 (6 %) |
| esc_status.esc[].esc_temperature | esc_data | any ESC reports non-zero temperature | 1/20 (5 %) | 1/20 (5 %) | 1/18 (6 %) |
| esc_status.esc[].esc_errorcount | esc_data | any ESC reports errors (> 0) | 1/20 (5 %) | 0/20 (0 %) | 0/18 (0 %) |
| actuator_outputs.output[] | esc_data | any output non-zero (load proxy) | 17/20 (85 %) | 17/20 (85 %) | 16/18 (89 %) |
| failure_detector_status.fd_motor | fault_events | flag raised at least once | 14/20 (70 %) | 1/20 (5 %) | 1/18 (6 %) |
| failure_detector_status.fd_imbalanced_prop | fault_events | flag raised at least once | 16/20 (80 %) | 0/20 (0 %) | 0/18 (0 %) |
| failure_detector_status.fd_battery | fault_events | flag raised at least once | 16/20 (80 %) | 0/20 (0 %) | 0/18 (0 %) |
| vehicle_gps_position.time_utc_usec | utc_date | non-zero (GPS time known) | 18/20 (90 %) | 18/20 (90 %) | 17/18 (94 %) |
| sensor_gps.time_utc_usec *(extra)* | utc_date | non-zero (GPS time known) | 14/20 (70 %) | 14/20 (70 %) | 13/18 (72 %) |
| param LND_FLIGHT_T_HI | lifetime_hours | non-zero | 19/20 (95 %) | 7/20 (35 %) | 7/18 (39 %) |
| param LND_FLIGHT_T_LO | lifetime_hours | non-zero | 19/20 (95 %) | 15/20 (75 %) | 15/18 (83 %) |
| logged messages, level ERROR or worse *(extra)* | fault_events | at least one | 20/20 (100 %) | 7/20 (35 %) | 7/18 (39 %) |

| Counter | Derivable, all logs | Derivable, airborne logs |
|---|---|---|
| aircraft_key | 20/20 (100 %) | 18/18 (100 %) |
| utc_date | 18/20 (90 %) | 17/18 (94 %) |
| flight_time (needs a flight) | 18/20 (90 %) | 18/18 (100 %) |
| arm_cycles | 18/20 (90 %) | 18/18 (100 %) |
| landings (needs a flight) | 18/20 (90 %) | 18/18 (100 %) |
| battery_energy (needs a flight) | 18/20 (90 %) | 17/18 (94 %) |
| esc_data (needs a flight) | 1/20 (5 %) | 1/18 (6 %) |
| fault_events | 16/20 (80 %) | 14/18 (78 %) |
| lifetime_hours | 15/20 (75 %) | 15/18 (83 %) |

### px4 / vtol

20 logs, 0 unreadable, 18 airborne; median length 395 s. Firmware: v1.11: 3, v1.12: 1, v1.13: 2, v1.14: 6, v1.15: 4, v1.16: 2, v1.18: 1, v2.0: 1.

| Field | Serves | 'Real value' means | Logs with field | Logs with real values | Real values, airborne logs |
|---|---|---|---|---|---|
| info sys_uuid | aircraft_key | non-empty and not all zeros | 20/20 (100 %) | 20/20 (100 %) | 18/18 (100 %) |
| vehicle_status.arming_state | arm_cycles | ARMED (2) observed | 20/20 (100 %) | 19/20 (95 %) | 18/18 (100 %) |
| vehicle_status.armed_time | arm_cycles | non-zero timestamp | 17/20 (85 %) | 15/20 (75 %) | 15/18 (83 %) |
| vehicle_status.takeoff_time | flight_time | non-zero timestamp | 17/20 (85 %) | 15/20 (75 %) | 15/18 (83 %) |
| vehicle_land_detected.landed | landings | changes state (both values seen) | 19/20 (95 %) | 18/20 (90 %) | 18/18 (100 %) |
| battery_status.discharged_mah | battery_energy | > 0 mAh | 19/20 (95 %) | 19/20 (95 %) | 17/18 (94 %) |
| battery_status.voltage_v | battery_energy | > 0 V | 19/20 (95 %) | 19/20 (95 %) | 17/18 (94 %) |
| battery_status.current_a *(extra)* | battery_energy | > 0 A | 19/20 (95 %) | 18/20 (90 %) | 17/18 (94 %) |
| battery_status.cycle_count | battery_energy | > 0 | 19/20 (95 %) | 1/20 (5 %) | 0/18 (0 %) |
| battery_status.state_of_health | battery_energy | > 0 % | 19/20 (95 %) | 0/20 (0 %) | 0/18 (0 %) |
| battery_status.serial_number *(extra)* | battery_energy | non-zero | 18/20 (90 %) | 0/20 (0 %) | 0/18 (0 %) |
| esc_status.esc[].esc_rpm | esc_data | any ESC reports non-zero rpm | 2/20 (10 %) | 1/20 (5 %) | 1/18 (6 %) |
| esc_status.esc[].esc_temperature | esc_data | any ESC reports non-zero temperature | 2/20 (10 %) | 1/20 (5 %) | 1/18 (6 %) |
| esc_status.esc[].esc_errorcount | esc_data | any ESC reports errors (> 0) | 2/20 (10 %) | 1/20 (5 %) | 1/18 (6 %) |
| actuator_outputs.output[] | esc_data | any output non-zero (load proxy) | 20/20 (100 %) | 20/20 (100 %) | 18/18 (100 %) |
| failure_detector_status.fd_motor | fault_events | flag raised at least once | 12/20 (60 %) | 0/20 (0 %) | 0/18 (0 %) |
| failure_detector_status.fd_imbalanced_prop | fault_events | flag raised at least once | 15/20 (75 %) | 0/20 (0 %) | 0/18 (0 %) |
| failure_detector_status.fd_battery | fault_events | flag raised at least once | 15/20 (75 %) | 0/20 (0 %) | 0/18 (0 %) |
| vehicle_gps_position.time_utc_usec | utc_date | non-zero (GPS time known) | 17/20 (85 %) | 16/20 (80 %) | 15/18 (83 %) |
| sensor_gps.time_utc_usec *(extra)* | utc_date | non-zero (GPS time known) | 14/20 (70 %) | 13/20 (65 %) | 12/18 (67 %) |
| param LND_FLIGHT_T_HI | lifetime_hours | non-zero | 19/20 (95 %) | 12/20 (60 %) | 11/18 (61 %) |
| param LND_FLIGHT_T_LO | lifetime_hours | non-zero | 19/20 (95 %) | 19/20 (95 %) | 18/18 (100 %) |
| logged messages, level ERROR or worse *(extra)* | fault_events | at least one | 20/20 (100 %) | 6/20 (30 %) | 5/18 (28 %) |

| Counter | Derivable, all logs | Derivable, airborne logs |
|---|---|---|
| aircraft_key | 20/20 (100 %) | 18/18 (100 %) |
| utc_date | 16/20 (80 %) | 15/18 (83 %) |
| flight_time (needs a flight) | 18/20 (90 %) | 18/18 (100 %) |
| arm_cycles | 19/20 (95 %) | 18/18 (100 %) |
| landings (needs a flight) | 18/20 (90 %) | 18/18 (100 %) |
| battery_energy (needs a flight) | 19/20 (95 %) | 17/18 (94 %) |
| esc_data (needs a flight) | 1/20 (5 %) | 1/18 (6 %) |
| fault_events | 15/20 (75 %) | 14/18 (78 %) |
| lifetime_hours | 19/20 (95 %) | 18/18 (100 %) |
