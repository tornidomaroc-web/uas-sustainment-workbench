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
- **Before an aircraft's first log, its time in service is not known.** The hours before
  the first log are one total, reached by that log; the records hold no date to place them
  on. So at an `as_of` before the first dated log the time in service, the 100-hour
  inspection item and the board read not known, with the sentence that says why, unless an
  open work order or a life limit already past grounds the aircraft. An aircraft in that
  state is not serviceable and not unserviceable: the tool cannot tell, and says so. The
  window runs from the entry of the hours before the first log to that log, and it cannot
  be made smaller than the records it holds: an inspection done before the first log states
  the hours it was done at, and the write rules accept it only once the hours before the
  first log are on record at an earlier date (an inspection stating more hours than the
  aircraft then had is refused), so the hours entry is dated no later than the earliest such
  inspection. The synthetic fleet enters its hours the day before its earliest inspection,
  months before its first logs, so its board before August 2026 is mostly not known; only an
  aircraft whose first records are its first log has no window. Records from before the first
  log (an inspection with its hours stated) are kept as stated and not checked against a time
  the tool cannot compute; the seed states them on one steady rate of flying, its own
  assumption, marked as such in `fleet/synthetic.py`.
- **An annual inspection counts toward the 100-hour rule** (14 CFR 91.409(b), "an annual or
  100-hour inspection"); the later of the two starts the interval. That is the only
  satisfaction rule; nothing in `fleet.toml` lets a lesser inspection satisfy a greater.
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
- **A cue is shorter than its sentence, never other than it, and never the record.** The
  short cue served with `?cues=true` drops the aircraft key, the regulation, the tolerance
  explanation, a calendar item's day count and a work order's description, and keeps the
  part or inspection, the number, the unit and the state; it is generated, never edited, and
  the tests refuse one that names another part, another number, another unit or a state its
  sentence does not say. It is a display aid: the sentence is the record's word, the
  evidence pack does not carry cues, and the assistant does not read them.

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
  close) cannot be superseded until those are corrected first, and a back-dated entry or a
  retraction that would make an already recorded later entry impossible is refused with
  that entry's id. Some histories therefore have to be entered in a fixed order: the hours
  before the first log before an inspection that derives its hours from them, a later
  install retracted before the remove it relied on. The tool says which entry stands in
  the way, not the order to take.
- **A correction keeps the date of the entry it corrects.** A correction with another
  `occurred_utc` is refused, because between the two dates neither entry would count; to
  move a date, the entry is retracted and a new one recorded, and the two are then not
  linked as correction and corrected. Among entries at one instant a correction takes the
  place of the entry it corrects.
- **A part that has reached a life limit cannot be fitted; a part that crosses one while
  fitted stays.** An install is refused when the part has reached any of its limits on the
  install's own date, exactly or past it (14 CFR 43.10(c): the control method must deter
  installation "after it has reached its life limit"), with the usage the
  records held by then on every airframe it had been fitted to. Reached means nothing left:
  300 of 300 cycles, the last day of a calendar life. That is not the board's boundary,
  which asks whether a fitted part may keep flying and calls remaining 0 due soon. What this
  does not do: it does not undo an install when a log ingested later puts that date's usage
  at or over the limit; the board reports the part as past its limit, and the install
  stands. A flight with no UTC start counts for no component, so it never puts a part at or
  past a limit, here or on the board. And it judges the projection, not the world: a part
  whose true usage is higher than its records say is fitted on its records.
- **A state at `as_of` is valid time, not record time.** The records at a time are folded
  from the live entries that had occurred by then, with liveness decided over the whole
  ledger first, as known now. So a query about March answers "what was true in March, as
  the ledger stands today": a retraction entered in September still governs it. The
  ledger keeps `recorded_utc`, so "what did the board show on that day" could be answered
  too; nothing asks it yet, and no route or tool takes a record time. Before 0.2.0 every
  live entry was folded whatever its date; see CHANGELOG.md.
- **Writes are judged at the entry's own date, and against valid time only.** A new entry
  is checked against the records as they stood when it happened, then every later entry of
  the same subject is checked again with it in place. What this does not do: it does not
  ask when anything was recorded, so it cannot tell a back-dated entry from one entered on
  the day; and it checks only what the projection can see, so a well-formed false
  statement is still accepted (above). A history that needs a fixed order of entry is
  refused one entry at a time, naming the entry in the way, and the refusal can send a
  person through a retraction and a re-entry to change one value.

## Limits of the evidence pack

- **It gathers; it does not find.** Every item of OSO #03 is marked supported, partly or
  not evidenced by this workbench. None of that is a finding of compliance, and the pack
  says so in its first lines; the authority decides what the records show.
- **Most of OSO #03 is out of reach of a records tool.** Instructions, staff
  authorisation, competence, training, release to service, a procedure manual and
  third-party validation are not held and cannot be evidenced. The pack can evidence the
  maintenance log with why each thing was done, the usage behind the schedule, and the
  life-limited part history, and it lists every other item as not evidenced.
- **The programme is the operator's defaults, not the designer's instructions.** The
  intervals come from `fleet.toml` with the public source each cites. An authority would
  ask for the designer's instructions for continuing airworthiness, which the workbench
  does not hold.
- **The pack inherits every limit above.** Time in service is only as complete as the logs
  plus the counter, entries are what a person typed, and the hash proves that the records
  did not change, not that they are true.
- **The sources are paraphrased.** The JARUS texts may be used but not copied without
  permission, so each item is a paraphrase in this project's words with document, edition
  and page; a reader checks the wording against the cited page.

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
- **A recording is evidence of one run; a seed change makes it stale.** Five of the nine
  runs were recorded again in 0.3.0 after the synthetic fleet changed, with the same model
  and weights at the same computation date (CHANGELOG.md says which and why); the earlier
  files are in the history. Between 0.2.0 and 0.3.0 one run, made before the
  `list_aircraft` tool was dated, was kept byte for byte with its hash pinned and a replay
  allowance for its undated query; that run was among the five, so no allowance remains and
  every recorded call is held to exactly the query it recorded.
- **Hours and cycles cannot be projected.** Only calendar limits have a date, so "what is
  due before Friday" is answered for calendar items and stated as unknown for the rest.
- **The ledger checks catch what they can match.** A name written as an initial and a
  surname, a work order state and a work order id must appear in some fetched record; a
  name written any other way is not recognised as a name. "Entry #N" needs a ledger fetch
  that holds that id, whatever else was fetched; a bare "#N" counts as an entry only once
  the ledger was fetched, so a citation such as "OSO #3" in an answer that never touched
  the ledger is judged as a number, and a fabricated bare "#N" there is judged the same
  way. A superseded entry named without its successor is caught; a superseded fact
  restated without naming the entry is not. The entries under each answer exist so a
  reader can check that.
- **No airworthiness decision.** The assistant reports the board state and its reasons and
  says that the workbench does not certify airworthiness.

## Limits of the ArduPilot evidence

Everything ArduPilot-side rests on one aircraft, ten logs on two days in July 2018,
`ArduPlane V3.9.0-beta1`, flown with `ARMING_REQUIRE=0` (always armed, so no arm events
are ever logged) and `BATT_MONITOR=0` (no battery data). It shows that flight time,
landings, UTC date, the lifetime counter and reconciliation work on real ArduPlane logs.
It shows nothing about arm cycles, battery energy, `ERR` messages, `ARM`/`BAT` messages or
current ArduPilot firmware; those paths are tested only on synthetic logs.
