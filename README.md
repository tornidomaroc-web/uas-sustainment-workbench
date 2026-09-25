# UAS Sustainment Workbench

[![ci](https://github.com/tornidomaroc-web/uas-sustainment-workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/tornidomaroc-web/uas-sustainment-workbench/actions/workflows/ci.yml)

An open-source maintenance and readiness tool for small civil unmanned aircraft fleets
(inspection, mapping, research, training). It will turn PX4 and ArduPilot flight logs into
usage counters (flight hours, cycles, landings, battery use) and track component life and
inspections against them.

## What exists today

**A data field census.** Before designing any counter, the project measured which log
fields are really present and filled in public logs: 40 PX4 Flight Review logs (20
fixed-wing and 20 VTOL aircraft, one log each), the ten ArduPilot DataFlash logs of the CMU
ALFA dataset, and two fields across all 31,550 eligible logs in the Flight Review index.
Full tables: [`results/census.md`](results/census.md). Method: [`results/README.md`](results/README.md).

What the numbers say, for logs in which the aircraft flew:

| Counter | PX4 (36 flown logs) | ArduPilot (ALFA, 9 flown logs) |
|---|---|---|
| Aircraft id, flight time, landings | 36/36 | 9/9 (id masked in fixtures) |
| Arm cycles | 36/36 | 0/9: this aircraft never logs arming |
| Battery energy used | 34/36 | 0/9: battery monitor disabled |
| Lifetime autopilot hours | 33/36 | 9/9 |
| ESC telemetry (rpm, temperature) | 2/36 | 0/9 |
| Smart-battery cycles, health, pack id | 0/36 | 0/9 |

So v0.1 builds on flight time, landings, arm cycles, battery energy and lifetime hours, and
leaves ESC and smart-battery counters out.

One result shapes the design. On ALFA, flight time derived from a log agreed with the
autopilot's own lifetime counter to within 13 s on 6 of 9 consecutive flights; the other 3
revealed flights the logs missed. A test runs this check on real log excerpts
([`tests/test_alfa_fixtures.py`](tests/test_alfa_fixtures.py)).

## What comes next

1. `FlightRecord`: one normalised record per flight from PX4 and ArduPilot logs, with a
   reconciliation check against the autopilot's lifetime counters that flags missing logs.
2. A component-life engine (hours, cycles, calendar, whichever comes first) and a readiness
   board.
3. An assistant that answers maintenance questions through the tool's own API.

## Scope and non-goals

- Civil fleet sustainment only: usage counters, component life, inspections, readiness.
- No payload, targeting, engagement or counter-UAS functions.
- Public and synthetic data only. Not endorsed by, or built for, any armed force.
- No raw flight logs in this repository. The only log files are three short excerpts with
  positions and device ids removed. See [DATA.md](DATA.md).

## Run it

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev,fetch]"
ruff check . && mypy && pytest                     # no network, no downloads needed

uasw-census px4-select && uasw-census px4-fetch   # ~1.3 GB of public logs, polite rate
uasw-census alfa-fetch                            # ten ALFA logs, ~345 MB
uasw-census run                                   # writes results/census.{md,json}
```

Downloaded logs go to `data/raw/`, which git ignores.

## Licence

Code: Apache-2.0. Data and fixtures keep their own licences and attributions; see
[DATA.md](DATA.md).
