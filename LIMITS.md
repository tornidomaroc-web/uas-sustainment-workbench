# What the logs cannot tell, measured

Every number here was measured on 2026-09-25 on public logs: 40 PX4 Flight Review logs (20
fixed-wing and 20 VTOL aircraft, one log each), 3 consecutive logs of one further PX4 VTOL,
and the 10 ArduPilot DataFlash logs of the CMU ALFA dataset. `results/census.md` has the
field-level tables. This file is about what that means for a maintenance tool.

## Limits of the data itself

- **Only the flight controller is identified.** PX4 logs carry the controller's `sys_uuid`
  (in 100 % of logs); ArduPilot logs print the controller's id in a boot message. Nothing
  identifies the airframe, motors, propellers, servos, ESCs or battery pack. Swap the
  controller into another airframe and the counters follow the wrong aircraft. The
  operator has to tell the tool which airframe a log belongs to; `reconcile()` takes that
  as `aircraft_key`.
- **No battery pack identity, cycle count or health** in public logs: PX4 `cycle_count`
  and `state_of_health` were present in 95 % of logs and filled in 1 of 40; `serial_number`
  never. Pack assignment and cycle counting must be done by the tool and marked as such.
- **No ESC data** in 38 of 40 PX4 logs and 10 of 10 ALFA logs. Motor counters from ESC
  telemetry are out of v0.1.
- **Parts, repairs, inspections and calendar age** are never in a log.

## Limits of what a single log can say

- **A log is not a flight.** Logging starts and stops on its own rules (PX4 by default at
  arming and disarming; ArduPilot with its own logging settings). Flight can happen after
  a log stops: on ALFA, 355 s of flight followed the end of `2018-07-30_16-46-36` with no
  log at all. Only the autopilot's lifetime counter shows it, hence `reconcile()`.
- **PX4 arm cycles**: because logging starts at arming, the arming transition is not in
  the log in 35 of 40 logs; the tool counts a log that opens armed as one cycle.
- **UTC date** needs a GPS fix: unknown in 6 of 40 PX4 logs (bench runs, indoor flights).
- **Battery energy**: PX4 reports mAh in 38 of 40 logs; Wh is integrated from voltage and
  current, missing in 3 of 40. ArduPilot logs nothing when `BATT_MONITOR=0`, which is how
  the ALFA aircraft flew.
- **Fault events** are what the autopilot chose to report: PX4 ERROR-level messages and
  failure-detector flags (15 of 40 logs have at least one), ArduPilot `ERR` messages and
  crash flags. Absence of a reported fault is not absence of a fault.

## Limits of reconcile()

- **It needs the next log of the same aircraft.** The counter a log booted with is only
  meaningful against a later boot. The last log of every aircraft is always "unchecked",
  and the 40 census logs, one per aircraft, produce no coverage at all.
- **PX4 saves its counter only at disarm** (`LandDetector.cpp`, `commit_no_notification`),
  so it is never written inside a log, and a power loss while armed loses that session's
  flight time from the counter for good. Measured on 3 consecutive logs of one VTOL: the
  counter advanced 4612.0 s over a log showing 4611.4 s of flight (0.6 s apart). The third
  upload was a byte-identical duplicate of the second; the tool names it instead of
  reporting −5150 s.
- **ArduPilot writes its counter into the log every 30 s** (`AP_Stats`,
  `flush_interval_ms`), so the derivation can be checked inside one log: on ALFA the
  counter advanced 304, 260 and 1407 s against 312, 260 and 1395 s derived. The 30 s flush
  is the tolerance; a counter can under-count by up to one flush per power cycle.
- **PX4 has no boot counter**, so unlogged boots between two logs cannot be counted; on
  ArduPilot `STAT_BOOTCNT` shows them.

## Limits of the life engine

- **Usage is only as complete as the logs plus the counter.** Flight time comes from the
  logs, plus the flight `reconcile()` found no log covers; on ArduPilot that credit is
  short by up to one 30 s flush, and on PX4 a power loss while armed leaves flight the
  counter never saw. Hours before the first log are an operator record.
- **A cycle is one flight record.** No public log carries a battery cycle count that is
  filled in, and a log is not a flight (above), so one record with flight time counts as
  one cycle: one take-off and one landing. A flight with several landings is still one.
- **Which part is on which airframe is an operator record.** Logs identify the flight
  controller only, never a battery pack, a motor or a propeller, so every installation
  window is entered by hand and a flight with no UTC start cannot be assigned to any
  component; it counts for the airframe and a note says so.
- **Calendar limits assume the stated in-service date** and run to the end of the month N
  months later. A part with no known in-service date has no calendar item.
- **The limits are placeholder defaults.** Every value in `fleet.toml` cites where its
  form comes from, none is a manufacturer's number, and Part 107 sets no inspection
  interval at all; the two inspections borrow the manned Part 91 shape so the tolerance
  and calendar logic have a public source.

## Limits of the maintenance ledger

- **Entries are what a person typed.** The name and role in `entered_by` are recorded as
  given and never verified; there is no signature, no certificate number check, no user
  account and no role. 14 CFR 43.9 asks for a signature and a certificate number, and the
  tool has neither.
- **The write protection is a shared secret or a network address.** With
  `UASW_WRITE_TOKEN` set, anyone holding the token may write from anywhere the service is
  reachable. Without it, writes are accepted only from loopback addresses. That rule cannot
  work in the container: a request from the host arrives from the Docker bridge gateway,
  measured as 172.17.0.1 on this machine, so compose passes the token from the environment
  and the CI container job generates a random one per run. Neither protects against a
  process on the same machine, a reverse proxy that hides the client address, a leaked
  token, or reading, which needs nothing. Nothing is encrypted in transit.
- **The ledger is append-only in the application, not in the file.** Anyone with the
  SQLite file can alter it. An audit that must survive that needs a store this tool does
  not provide.
- **Validation is against the projection, not the world.** A well-formed false statement
  (an inspection that never happened, a part that is not really on the aircraft) is
  accepted. Only impossible transitions are refused.
- **Derived time in service is only as good as the logs.** When a person records an
  inspection without stating the hours, the tool fills in the time in service it computes
  at that date and marks the value derived; it carries every limit in "Limits of the life
  engine" above.
- **Corrections are ordered by dependency, not by time.** An entry with later live
  entries depending on it (a registration with installations, an open work order with a
  close) cannot be superseded until those are corrected first.

## Limits of the assistant

- **It reads; it never computes.** Its only tools are the service's GET endpoints, and the
  computation date is pinned outside the model. Anything not in a fetched record is, by
  the system prompt, something it must say it does not have.
- **The grounding check is a filter, not a proof.** It rejects an answer that states a
  number, a component or aircraft id, or a date absent from the fetched records, with one
  allowance for integers up to ten, which may be counts made while phrasing. It cannot
  tell a wrong sentence built from correct values. The records under each answer exist so
  a reader can check that.
- **Recorded runs are one model's output on one date.** They name the model tag, the digest
  of its weights, the recording date and the computation date. Re-recording with another
  model or another date gives different sentences; the test suite only guarantees that the
  records behind each recorded answer are what the current code returns.
- **Hours and cycles cannot be projected.** Only calendar limits have a date, so "what is
  due before Friday" is answered for calendar items and stated as unknown for the rest.
- **No airworthiness decision.** The assistant reports the board state and its reasons and
  says that the workbench does not certify airworthiness.

## Limits of the ArduPilot evidence

Everything ArduPilot-side rests on one aircraft, ten logs on two days in July 2018,
`ArduPlane V3.9.0-beta1`, flown with `ARMING_REQUIRE=0` (always armed, so no arm events
are ever logged) and `BATT_MONITOR=0` (no battery data). It shows that flight time,
landings, UTC date, the lifetime counter and reconciliation work on real ArduPlane logs.
It shows nothing about arm cycles, battery energy, `ERR` messages, `ARM`/`BAT` messages or
current ArduPilot firmware; those paths are tested only on synthetic logs.
