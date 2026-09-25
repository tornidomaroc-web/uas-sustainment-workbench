# Data sources, licences and privacy

## What this repository contains

1. Aggregate counts (`results/census.json`, `results/census.md`): how many logs carry a
   field and how many carry real values. No positions, device ids or log ids.
2. Three short excerpts of ALFA logs used as real-log test fixtures (`tests/fixtures/alfa/`,
   about 0.9 MB in total), described below.
3. One excerpt of a PX4 Flight Review public log (`tests/fixtures/px4/`, 0.3 MB), described
   below, with the one log link this repository contains.

No other flight log is stored. A test (`tests/test_repo_hygiene.py`) fails CI if any other
log file is tracked, and `tests/test_alfa_fixtures.py` checks that the excerpts hold no
position and no device id.

## Sources

### PX4 Flight Review public logs

- Where: <https://review.px4.io>. Index: `https://review.px4.io/dbinfo` (redirects to a
  gzip-compressed JSON on `cdn.logs.px4.io`); files: `https://review.px4.io/download?log=<id>`.
- Licence: logs marked public at upload are "publicly available under CC-BY PX4 license"
  (upload page text, <https://github.com/PX4/flight_review/blob/main/app/plot_app/templates/upload.html>;
  maintainer confirmation: <https://discuss.px4.io/t/license-for-the-dataset/42819>).
  Attribution: PX4 Flight Review contributors, CC BY 4.0.
- Access: through the project's own script `app/download_logs.py` (BSD-3-Clause), pinned to
  commit `ea5c16a3bb939f49e50296f953efac40c2456489` and checked by SHA-256 before it runs,
  with its default 6 s delay between downloads. `robots.txt` disallows crawling, so nothing
  here crawls pages.
- Privacy: public logs carry GPS tracks and a hardware id of a real person's aircraft. They
  are downloaded to `data/raw/px4/` (git-ignored) and never redistributed.

### CMU ALFA dataset (fixed-wing, ArduPlane)

- Where: <https://doi.org/10.1184/R1/12707963.v1> (KiltHub/figshare), file `dataflash.zip`.
- Citation: Keipour, Mousaei, Scherer, "ALFA: A Dataset for UAV Fault and Anomaly
  Detection", *The International Journal of Robotics Research*, 2021.
- Licence: the figshare record states **CC BY 4.0**; the dataset's own `README.txt` states
  **CC0**. This project follows the stricter of the two, CC BY 4.0, and attributes the authors.
- Contents measured on 2026-09-25: `dataflash.zip` holds ten `.bin` DataFlash logs, from
  2018-07-18 (4) and 2018-07-30 (6). The 47 fault sequences the dataset is known for are in
  `processed.zip` (ROS bag, CSV, MAT), not in DataFlash form.
- Access: HTTP range requests extract only the ten `.bin` members (about 144 MB of 541 MB);
  zipfile verifies each member's CRC-32.
- Privacy: flight tracks around the collection site; kept in `data/raw/alfa/`, git-ignored.

## Test fixtures: ALFA excerpts

`tests/fixtures/alfa/2018-07-30_16-30-14.bin`, `2018-07-30_16-46-36.bin` and
`2018-07-30_17-28-50.bin` are excerpts of the ALFA DataFlash logs with the same names.

- Attribution: A. Keipour, M. Mousaei, S. Scherer, "ALFA: A Dataset for UAV Fault and Anomaly
  Detection", Carnegie Mellon University, <https://doi.org/10.1184/R1/12707963.v1>,
  licensed CC BY 4.0 (<https://creativecommons.org/licenses/by/4.0/>).
- Changes made (produced by `uasw-census alfa-excerpt`, `src/uas_workbench/census/excerpt.py`):
  only the message types `PARM`, `MSG`, `MODE`, `STAT` and `GPS` are kept, each copied byte
  for byte; `GPS` latitude, longitude, altitude and course are set to zero; the flight
  controller's unique id in boot messages is replaced with `XXXXXXXX`. Every other message
  type, including all attitude, position and sensor data, is removed.
- Why these three: they are consecutive flights of the same aircraft, so they test both a
  log that agrees with the autopilot's lifetime counter and one that does not.
- The excerpts are not endorsed by the dataset authors.

## Test fixture: one PX4 Flight Review excerpt

`tests/fixtures/px4/flight_review_board_validation_2026-06-12_excerpt.ulg` is an excerpt of
the public log <https://review.px4.io/plot_app?log=60f0a65f-fea3-46cc-b8db-23ac20adb24c>.

- Attribution: PX4 Flight Review contributors, public log linked above, a basic flight
  validation of 2026-06-12; the uploader gave no name. Licensed CC BY 4.0
  (<https://creativecommons.org/licenses/by/4.0/>) by the upload terms of Flight Review.
  CC BY 4.0 asks for the creator, a link, the licence and a note of changes, which this
  entry gives; the log's own title and hardware name are left out because this repository
  names no real aircraft or drone product.
- Why this log, and why its link is here: this repository otherwise publishes no log id,
  because a public log page shows the flight track and the hardware id of a real person's
  aircraft. This log was uploaded as a board vendor's validation flight for a PX4 upstream
  pull request, described as such in its own metadata, so linking it does not expose a
  private individual, and the link satisfies the attribution CC BY 4.0 asks for. It is a
  quadrotor: the point of the fixture is real PX4 data in CI, not airframe class.
- Changes made (`uasw-census px4-excerpt`, `src/uas_workbench/census/excerpt.py`): only the
  topics `vehicle_status`, `vehicle_land_detected`, `battery_status`, `vehicle_gps_position`,
  `sensor_gps` and `failure_detector_status` are kept (this log has no `battery_status`);
  every latitude, longitude, altitude, course and heading field is set to zero; the
  `sys_uuid` info message, the hardware name, hardware subtype and firmware branch info
  messages (`ver_hw`, `ver_hw_subtype`, `ver_sw_branch`) and the multi-line info blocks
  (boot console output) are removed.
  Formats, parameters and logged text messages are kept unchanged.
- The excerpt is not endorsed by the uploader or by the PX4 project.

## Synthetic fleet

`uas_workbench/fleet/synthetic.py` generates the demo fleet from the seed in `fleet.toml`.
Its aircraft are named `SYN-01` to `SYN-07`, every record carries `synthetic: true`,
`licence: CC0` and an attribution naming the generator and seed, and no value in it is
taken from any real aircraft. Counter behaviour copies what LIMITS.md measured (ArduPilot
flushes every 30 s and logs a boot count; PX4 saves at disarm). Its maintenance records,
components and work orders are generated too and marked synthetic; the board states are
computed from them by the life engine, in the civil vocabulary of `fleet.toml`.

## Not used, and why

- ArduPilot autotest logs: no licence statement.
- ArduPilot forum attachments: CC BY-NC-SA 3.0 (forum terms); NonCommercial conflicts with
  an OSI licence.
