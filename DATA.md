# Data sources, licences and privacy

## What this repository contains

Aggregate counts only (`results/census.json`, `results/census.md`): how many logs carry a
field and how many carry real values. No positions, no device or vehicle ids, no log
contents, no raw log files. A test (`tests/test_repo_hygiene.py`) fails CI if a raw log
format is ever tracked.

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

## Not used, and why

- ArduPilot autotest logs: no licence statement.
- ArduPilot forum attachments: CC BY-NC-SA 3.0 (forum terms); NonCommercial conflicts with
  an OSI licence.
