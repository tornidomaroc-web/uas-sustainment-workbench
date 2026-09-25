# UAS Sustainment Workbench

An open-source maintenance and readiness tool for small unmanned aircraft fleets
(inspection, mapping, research, training), driven by PX4 and ArduPilot flight logs.

**Status: step 1, data field census.** Before any counter or data model is designed, this
step measures which log fields are really present and filled in public flight logs. The
result is in [`results/census.md`](results/census.md).

## Scope and non-goals

- Civil fleet sustainment only: usage counters, component life, inspections, readiness.
- No payload, targeting, engagement or counter-UAS functions, now or later.
- Public and synthetic data only. Not endorsed by, or built for, any armed force.
- Raw public logs are never stored in this repository. See [DATA.md](DATA.md).

## Run the census

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev,fetch]"

uasw-census px4-select     # snapshot the Flight Review index, choose candidates (seeded)
uasw-census px4-fetch      # size-check, then download via the official download_logs.py
uasw-census alfa-fetch     # extract the ten ALFA DataFlash logs by HTTP range
uasw-census run            # probe every local log; write results/census.{md,json}
```

Raw logs land in `data/raw/` and per-log results in `data/local/`; both are git-ignored.

## Develop

```bash
ruff check . && mypy && pytest
```

Tests build tiny synthetic ULog and DataFlash files in memory, so CI needs no network and
no real log ever enters the repository.

## Licence

Apache-2.0. Data sources keep their own licences; see [DATA.md](DATA.md).
