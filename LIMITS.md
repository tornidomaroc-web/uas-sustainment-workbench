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
- **A log's span is measured over the messages the tool reads.** For a ULog, from the start
  in its header to the last timestamp of the topics it reads; for a DataFlash log, from the
  first to the last timestamp of the message types it reads (PARM, MSG, STAT, GPS, EV, ARM,
  BAT, CURR, ERR). A log whose other messages run longer, or that holds only messages the
  tool does not read, reports a shorter span, down to 0.0 s, and since a log counts from its
  start plus that span, its usage counts that much earlier.
- **A log is a flight only if it holds recorded data, and that costs a second read in one
  case.** Definitions, parameters and boot text alone are refused. A ULog with data only in
  topics the tool does not read is parsed a second time, with every topic, to find that data;
  a large log of that kind is read in full twice.
- **A damaged DataFlash file is loud before it is refused; a file that is no log is not.**
  pymavlink's indexer prints one `bad header` line on standard error for every byte it cannot
  frame, from compiled C in the wheels published for Linux and Windows, so no redirect of
  Python's stderr sees them. A `.bin` with no plausible format definition anywhere (a printable
  name, format characters pymavlink knows, printable labels) is refused before pymavlink opens
  it, whatever its size: 0 lines for 16 B to 16 MiB of zeros, text, every byte value or random
  bytes, measured 2026-10-04 through the reader, `POST /ingest` and `uasw ingest`, where 0.5.1
  wrote one line per byte (13 for 16 B, 16 776 689 for 16 MiB of zeros; fewer for random bytes,
  whose chance headers stop the indexer early). A file that begins with a definition and runs
  into bytes pymavlink cannot frame, a damaged log or a crafted file, still gets one line per
  such byte before its 422 (1 048 049 for one definition and 1 MiB of zeros, as on 0.5.1), and
  a log behind k bytes of noise still parses and still gets k `bad header` lines and two
  `Skipped k bad bytes` lines (602 for 600 bytes of zeros before a fixture log). `POST /ingest`
  takes the write token or loopback, so the file is an operator's own or a trusted writer's.

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
  open work order or a life limit reached or past grounds the aircraft. An aircraft in that
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
- **A flight counts when its log ends, not when it lands.** A record holds no landing time:
  its flight time is a total airborne within the log, not an interval, and the UTC start is
  the start of the log, not the take-off. The end of the log (start plus span) is the last
  instant its flight can have ended, so the cycle and the hours count from there, never from
  the start and never partway through. The board therefore turns up to the length of the
  ground time inside the log after the wheels stopped: seconds on PX4, which logs from arming
  to disarming by default, and for as long as the log ran on under ArduPilot's own logging
  settings. It never turns before the flight is over, and the next log of the same flight
  controller cannot start before this one ends, so that aircraft's next logged flight never
  starts on usage not yet counted. The same instant decides
  which logs a dated answer lists: a log is a file that exists once it is closed. Until
  0.5.0 a log counted from its start, so the board could ground an aircraft as it took off
  on its last permitted cycle.
- **A flight with no recorded end is counted on the early side, and says so.** Three cases.
  A log with a UTC start always has an end, its span, even when its flight time is not
  known: it counts one cycle and no hours from that end. A log with no UTC start has no
  instant at all: it counts for the airframe at any date asked and for no component, with a
  note, as before. Flight that no log covers, found by `reconcile()` from the next boot's
  counter, was flown after the log it followed and ended at a time no record holds: it counts
  with that log, from that log's end, the earliest it can have been flown. That errs early by
  design, up to the gap to the next log, so a limit is never reported later than it was
  reached; and it is known only once the next log exists, so a dated answer between the two
  logs changes when that next log is ingested.
- **Reached means nothing left, in completed units.** A part at exactly its cycle or hours
  limit has used the whole of its life: it grounds the aircraft with a sentence that says it
  has reached its limit, and the ledger refuses to fit it, at the same count. Hours are
  compared after rounding to a thousandth of an hour (3.6 s), on the board and in the ledger
  alike, so "exactly" is that wide. A calendar life is counted in days and its last day
  completes at midnight UTC: on that day the part is within its life, valid on the board and
  accepted at an install; from the next day it is past on both. 0.3.0 and 0.4.0 refused an
  install on that last day while the board called the same day valid; 0.5.0 withdrew that
  refusal. What this does not cover: an inspection at exactly zero hours remaining still
  reads "due in 0.0 h", because 14 CFR 91.409(b) gives the 100-hour inspection a tolerance
  for the flight that overruns it and a life limit has none.
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
- **A flight logged while the aircraft was grounded is reported; the report says the records
  and the log disagree, not which is right.** Each log's start is compared with the board
  state the records give at that instant, and a known unserviceable, in maintenance or AOG
  is a finding. What the finding cannot know, each a reason it may be wrong about the world:
  - *Who knew what, when.* The state at the log's start is what the ledger says today about
    that time (valid time). An entry dated back after the flight makes the flight a finding,
    and a retraction unmakes one; the tool does not tell an entry made on the day from one
    made a month later, though the ledger keeps both dates.
  - *How precisely an entry is dated.* A log's start is to the second, from the GPS clock.
    An entry carries the time a person typed. A work order dated to a day, entered as
    midnight, turns every flight of that day into a finding, including flights before the
    defect was found; the finding is only as fine as the coarser of the two times.
  - *Whose log it is.* A log names the flight controller, never the airframe. A log filed
    under the wrong aircraft is judged against the wrong records.
  - *Whether the flight was permitted.* The tool holds no release to service and no permission
    for a check flight after maintenance, so a flight an operator authorised with a work order
    still open is reported like any other.
  - *What the log does not hold.* A log with no UTC start cannot be placed in time and is not
    judged. A log with 0 s airborne is not a flight (maintenance runs an aircraft on the
    ground) and one that cannot say whether the aircraft flew is not called a flight; neither
    is judged. Flight that no log covers has no start at all and is never judged, although
    it counts toward usage.
- **The instant judged is the log's start itself, not the moment before.** Everything dated
  up to and including that instant counts: an entry with the same time stamp, a calendar
  life that ended at that midnight, an earlier log that ended at that instant. The log's own
  flight does not, because a log counts from its end. This is the answer the board gives at
  `as_of=<the log's start>`, so each finding can be checked there. It also means an
  aircraft's first log is judged on a known state: the hours before the first log are a
  total reached by that log's start, so the state is known from that instant, and not known
  only before it.
- **Not known is never a finding, and is listed.** A log that starts when no maintenance
  record had been entered is not judged, with that reason; so is every log of an aircraft
  with no record at all. The two public showcase aircraft are never judged, by key, whatever
  the store holds: their flights are real people's, and the tool accepts no record for them.
  The answer carries how many logs were judged and each log that was not with why, so "no
  findings" is never read as "all clear" when little could be judged.
- **Where the finding is served, and where it is not.** Two routes, the evidence pack's
  usage section and the demo page. Not the board, the due list or the count of findings on a
  board row, which stay what they were: the board is the state now, and the answers the
  recorded assistant runs read are unchanged. Not the assistant, which has no tool for it,
  and not `/metrics`. Each answer judges every log on a board computed at that log's start,
  so the cost grows with the square of the number of logs; measured at 28 ms for the demo
  fleet's 34 logs (35 ms for the 40 of the 0.4.0 seed), and not tried on a large fleet.
- **The demo fleet shows no such flight because it is generated not to, which shows nothing
  about the finding.** Since 0.5.0 the generator draws every flight and then leaves out each
  log that would start while its aircraft is grounded, using this same comparison, so the
  demo's empty list is true by construction. What the finding reports is shown elsewhere:
  on hand-built aircraft in the tests, each state and each not-known case, and by a test that
  puts the left-out logs back and finds six of them reported. An aircraft left out of flying
  is also an aircraft with fewer logs: SYN-01 has three and SYN-05 two, and the reconciliation
  of each has that much less to compare.
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
- **The ledger is append-only in the application; in the file, a change is visible, not
  prevented.** Anyone with the SQLite file can still alter it. Every entry and every
  flight record is linked into a write journal in the transaction that writes it: one hash
  chain over both tables, a SHA-256 of each row's content in a canonical form (the columns the
  queries filter on and the record JSON parsed, so re-serialising a record changes nothing and
  moving an entry to another subject does), the previous link's hash and the link's own.
  `uasw verify` and `GET /journal/verify` recompute every hash from the rows as they are and
  name the first bad link and its kind. Measured on the seeded store (94 links), each done
  directly in SQLite with no store open: an entry's record edited is reported at its link as
  `content`, and so is an edit to its subject column alone; a flight deleted is `missing_row`
  at its link; a link inserted at position k with every hash computed correctly is
  `previous_hash` at k+1, and with wrong hashes at k; a row inserted with no link is
  `unjournaled` at the position after the last link; a link whose stored hash was edited is
  `link_hash`; a link removed is `sequence`; the journal emptied is `unjournaled` at 1, and the
  store does not rebuild it on open, because a rebuilt chain would verify as if nothing had
  happened. What is not detected, each measured to verify alone:
  - *A truncated tail.* The last links removed together with their rows is a shorter chain that
    is intact as far as it goes.
  - *A chain recomputed from the start.* Someone with the file and this code can edit a row and
    recompute every link after it, as the store would have. The chain has no secret and nothing
    outside the file, so the file cannot tell.
  - *An append made in the file.* A row inserted directly with its link computed as the store
    would is not told from one appended through the service: the journal has no key and the
    service has no identity to sign with.
  - *The aircraft rows and the file as a whole.* Aircraft keys, labels, licences and attributions
    are not journaled (the row is replaceable by design), and the file replaced by an earlier
    copy is a truncated tail.
  - *Before the migration.* A store written by 0.5.x is journaled when this version first opens
    it, each row linked as found after a note that says so, with the counts and the time; a
    change made to it before that is linked as found.

  Only a head kept outside the file tells the first two from an untouched store:
  `uasw verify --head H` reports whether H is in the chain, which a truncated or recomputed
  chain fails, and nothing in this tool keeps a head anywhere. Writing the head down after each
  session, where the file cannot reach it, is the operator's, and so is deciding who may hold
  the file. The line is "unchanged since head H", and that is all it says: a false statement
  linked at entry is linked false, and what the ledger refuses or accepts is unchanged. Whatever
  the file holds, verifying reports and never raises: a record that cannot be parsed or hashed
  (not JSON, nested too deep for the parser, a column turned into a BLOB), a note that is not
  the migration note, and a note on an entry or flight link, where the column is NULL and no
  hash covers it, are each a `content` break at that link. Verifying reads every row: 3 ms for
  the seeded store (94 links, median of 50 runs on 2026-10-07), and 2.4 s for a store of
  100,000 links (the seeded store plus copies of its entries, 78 MB; median of 5 runs on
  2026-10-11 in a cloud container, Python 3.12, SQLite 3.45.1; 3.9 s on 2026-10-08 on another
  container). A write no longer waits for it: the walk runs on the calling thread's own
  reading connection inside one read transaction, and the store file is in SQLite's WAL mode,
  so the walk reads a snapshot and the store's commit goes through beside it. Measured on the same 100,000-link store: a
  write issued 0.5 s into the walk took 1.9 ms (median of 5, 4.2 ms at most) where it takes
  1.0 ms alone and waited 3.86 s on 2026-10-08, when the walk held the store's write lock; a
  write made while another connection holds a read transaction took 1.4 ms, where in the
  rollback-journal mode it failed with the database locked (the defect PR #57 met). What WAL
  changes and what it does not, each checked by `tests/test_wal.py`:
  - *`GET /journal/verify` stays guarded as the writes are*, token or loopback, though it makes
    nothing wait: it is the costliest read the service has, every row on every call.
  - *One writer at a time, as before.* A second writer (`uasw record` beside a running service)
    waits up to 5 s (`PRAGMA busy_timeout`) for the write lock, then fails with the database
    locked; a refused commit is rolled back and moves nothing kept. SQLite's rollback journal
    had no wait at all, so that failure came at once.
  - *Durability is as it was.* `PRAGMA synchronous` is FULL, the WAL default: a journal link is
    on disk before the commit that wrote it returns. NORMAL would save one sync per commit and
    could lose the last commits to a power cut; the link is the record and is not traded.
  - *A reader on another connection sees a snapshot and makes the commit wait for nothing.*
    `PRAGMA data_version`, which the kept journal result and `/health` key on, moves on a
    commit from another connection and on nothing else (own commits, own rollbacks, another
    connection's rollback), under WAL as before. The walk that fills the kept result reads
    `data_version` and every row inside one read transaction, so a commit from another
    connection landing halfway through is not in it and the result is the state its version
    names (before that, from PR #57 on, such a commit could make one answer show a break no
    single state of the file had); the kept result equals a fresh walk of that state.
  - *Two side files while open.* `fleet.sqlite-wal` and `fleet.sqlite-shm` sit beside the file
    while any store has it open, in the `fleet-data` volume in the container; closing the last
    connection checkpoints the WAL into the file and removes both, so a closed store is one
    file, and every `uasw` command and a `uasw serve` stopped with Ctrl+C leave it so (checked
    on Windows). A process that is terminated, or a service stopped with Ctrl+Break on Windows,
    does not close the store: both side files stay, the `-wal` one holding commits not yet in
    the file. They are part of the store, never stray files to delete: the next store to open
    the file reads them and its close folds them in and removes them (checked: every write
    there, integrity ok). A copy of `fleet.sqlite` alone while a store is open, or after such a
    stop, is not a complete copy: README says how to copy a running store. Every connection
    must be on one host; the volume is, and a network file system is not supported.
  - *The mode is kept in the file.* A store opened once by this version stays in WAL when an
    earlier version opens it. 0.5.2 was checked: it reads such a store with the same answers
    and writes to it, but 0.5.x keeps no journal, so a row it writes has no link and from then
    on this version reports the journal broken at that row (`unjournaled`); the migration runs
    once, so the break stays. A store file marked read-only is verified as before, but its
    close cannot fold the WAL in, so both side files stay until a store opens it writable. A
    directory the process cannot write to was not measured; SQLite needs to create the side
    files there to open a WAL-mode file at all.
- **Threads never read through one connection; each read is one state of the file, and so
  is each records summary and evidence pack, while the board's responses are not.** Every request thread read through the store's
  one SQLite connection with nothing serialising the reads, and Python's `sqlite3` keeps one
  statement cache per connection, so two threads running the same SQL stepped the same prepared
  statement and read each other's rows: a GET answered 200 with another query's rows, 404 for
  an aircraft that is in the store, 500, or 503 naming a record that reads (issue #60; v0.5.2
  is affected). Measured through `uasw serve` in a process of its own, by
  `tests/test_concurrent_reads.py`: 200 requests per reader over five routes and a writer
  posting an entry every 20 ms, in a cloud container (Linux, Python 3.12, SQLite 3.45.1, 4
  cores, 2026-10-11, three runs each). On the version before this change, 2 readers answered
  23 to 39 of 400 requests wrongly (2 to 8 of them 200 with another query's body), 4 readers
  85 to 128 of 800, 8 readers 205 to 328 of 1,600; the issue's own figures, on a Windows
  desktop over 8 s, are of the same kind. With this change, 0 of 2,800 in every run, on Python
  3.12 and 3.13. Now the writing connection is used under the
  write lock only; every thread that reads opens a reading connection of its own on its first
  read, and each read (`aircraft`, `flights`, `entries`, the walk of the journal, every other)
  runs inside one read transaction on it, so one call never mixes two states of the file and
  no write waits for it. Every such transaction is a snapshot (`Store.snapshot`) labelled with
  the state it reads: the `PRAGMA data_version` of one more connection the store keeps for
  nothing else, read before and after the snapshot's first read and taken only when the two
  agree, which moves on every commit to the file, the store's own or another connection's,
  and on nothing else. A response that opens one snapshot around every read it makes is one
  state of the store, the projection and the kept journal result it shows included, and
  three do: the draft records summary of one aircraft, the fleet records summary and the
  draft evidence pack for OSO #03. Before, each of their reads was whole and the response was
  not: a correction committed between the entries and the projection of one summary gave the
  entries of the state before it with the superseded count and the journal length of the
  state after it (10 entries, 2 superseded, 95 links, where the store held (10, 1, 94) before
  the correction and (11, 2, 95) after; `tests/test_snapshot.py` on the version before).
  What is still not promised: the board's responses (`/aircraft`, `/aircraft/{key}` and its
  due list, `/fleet/due`, `/fleet/findings`, `/fleet/grounded-flights`, `/entries`,
  `/components`, the gauges behind `/metrics`) and the static export's `fleet.json` are built
  from several reads with no snapshot around them, so one can straddle a write that lands
  between two of its reads, as it always could; none of them carries a journal head or a
  hash that names one state, and each is rebuilt on the next request. The three answers the
  store keeps rather than computes on every call, the kept journal result behind the records
  summaries, what `/health` holds and the whole-ledger projection, are each labelled with
  the state they were computed from; a snapshot uses one only when its label is the
  snapshot's own, computes it on its own connection otherwise, and keeps it unless a newer
  state's answer is kept already. The store's own commit moves the three labels on with
  nothing computed when the kept answers described the state right before it, which the
  writing connection's `PRAGMA data_version` tells (it moves on another connection's commit
  and never on the store's own; read before the write and after the new label, it says
  whether any other commit landed in between), and drops them otherwise. So a snapshot never
  waits for a write and a write never waits for a snapshot: no read takes the write lock,
  and the one walk of the journal and the one read of the whole store that follow a commit
  from outside (1.3 s at 128,718 entries, above) run on the reading thread that meets it,
  where before they ran on the writing connection under the write lock and a write waited
  for them. What a write does pay: three `PRAGMA data_version` reads around its transaction
  (four when another connection committed since the store's last own write), two on the
  labelling connection and one or two on the writing one, each a C call that lets the
  interpreter's lock go, where a write made no such read before. Measured here, in a cloud
  container (Linux, Python 3.13, SQLite 3.45.1, 2026-10-11, medians; the version before in
  brackets): at the store, 30 writes 50 ms apart, with no reader 1.5 ms (1.4 ms); with 4
  threads building the per-aircraft summary without pause 9.7 ms (5.1 ms); with 8, 16.5 ms
  (10.1 ms). Through `uasw serve`, 20 `POST /entries` 100 ms apart: 28 ms alone (30 ms); 1.94 s
  with 8 unauthenticated readers looping over the per-aircraft summary, which answered 481
  requests meanwhile (1.59 s with 367 answered: those readers took the write lock for the kept
  journal result and were slower, so they left the writer more of the interpreter); 1.88 s with
  8 looping over the fleet summary (57 answered); 0.90 s with 8 looping over `/health`, which
  reads nothing but the kept answer (1.60 s, where `/health` took the write lock). The wait is
  the interpreter's one lock shared with the readers' threads, not the store, and every C call
  a write makes is one more turn it waits for: a caller who needs no token can slow every write
  by keeping the service's threads busy, on this version as on the one before, and the fleet
  summary gives such a caller nine aircraft's work for one request; nothing in the service
  limits a caller's rate, which is the reverse proxy's or the operator's to do. A
  `wal_checkpoint(TRUNCATE)`, which every `uasw` command and every store
  runs on close, moves other connections' `data_version` as a commit does, so a `uasw verify`
  run beside the service makes the service walk the journal and read the whole store once more
  on its next call, to the same answers. The reading connections are closed by `Store.close`,
  from the closing thread, after the threads that read are done: `uasw` closes its store when
  the command ends, `uasw serve` after the server has stopped; a read after the close is refused
  as a read on a closed connection is, and a closed store is one file, as before. A store in
  memory has one connection: a snapshot there holds the write lock for its length, so threads
  read one at a time and a write issued during a summary waits for it and lands after it.
- **Two entries appended at once through one store are checked one after the other; through two
  processes they are not.** An entry is checked against the records and then written, and the
  check and the write hold the store's write lock together, so a second request served beside
  the first is checked against the records with the first in them: two corrections of one
  entry posted at once give one 201 and one 409. Before, both were checked against the records
  as they were before either wrote and both were accepted (60 of 60 such pairs through `uasw
  serve` on Windows, Python 3.12, 2026-10-11, and 45 of 60 on the version before). Reads go on beside the check on their own
  connections. The lock is the process's own: a `uasw record` run beside a running service, or
  two `uasw` commands at once, can still both pass their checks before either writes, so record
  through one of them at a time.
- **A record the store cannot read leaves every projected record unknown, not one.** Liveness
  is decided over the whole ledger and a component move names another aircraft, so one entry
  that cannot be read (not JSON, nested too deep for the parser, a field missing or of the
  wrong type, a time with no UTC offset, details the fold cannot use) leaves no projected
  record of any aircraft known, and one flight record that cannot be read leaves the usage of
  its aircraft, and of every component that has been on it, unknown. The reads say so rather
  than answer without it: `GET /health` is `degraded` with the record named. It answers from
  what the store already knows and reads no record on a call: the whole store is read once at
  startup and again only on the first call after another connection has committed a change to
  the file (an edit made with sqlite3, or the repair); a write through the store is readable
  by construction and changes nothing there. Measured on 2026-10-08 on an 8-core desktop,
  Python 3.12, a store of 128,718 entries: 4.1 ms per call, median of 5 (4.3 ms before this
  change, when /health read no record either); 1.3 s for the one call after an outside
  commit, median of 5; startup 25 s, as before. The container above took 3.1 s for that whole
  read at 111,440 entries, past the 3 s timeout of the image's health check, which is why a
  call does not repeat it. Every read that depends on the record is 503 with the same
  sentence, a write judged against the projection is refused the same way with nothing
  written, `uasw`
  prints `refused (503)` and exits 1 (`uasw ask` too, before the model is asked
  again; issue #46), and the evidence pack is written with the dependent
  values unknown and the items they supported not evidenced. Nothing here repairs the record:
  `uasw verify` names its link, and restoring the file from a copy or fixing the row is the
  operator's. A record that reads but is false is not told from a true one, as above.
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
- **A part that has reached a life limit cannot be fitted; a part that reaches one while
  fitted stays on record.** An install is refused when the part has reached any of its limits
  at the install's own date, exactly or past it (14 CFR 43.10(c): the control method must deter
  installation "after it has reached its life limit"), with the usage the
  records held by then on every airframe it had been fitted to. Reached means nothing left:
  300 of 300 cycles, the whole of the hours, the end of the last day of a calendar life.
  Since 0.5.0 that is the board's boundary too: what cannot be fitted grounds the aircraft
  it is fitted to. What this
  does not do: it does not undo an install when a log ingested later puts that date's usage
  at or over the limit; the board reports the part as at or past its limit, and the install
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
  says so in its first lines; the authority decides what the records show. The flights its
  usage section lists as logged while the records show the aircraft grounded are
  disagreements between a log and the ledger, reported as that and nothing more.
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
- **The records summary lists; it does not judge.** `uasw records` sets what the workbench
  holds against the three record-keeping points of UAS.SPEC.050(1)(g) of Regulation (EU)
  2019/947 and says, for each, only whether records that bear on it are held: training records
  never (none are held), maintenance activities when the ledger holds an entry about the
  aircraft, operations partly when a log is held (a log is information on an operation and
  what the autopilot reported; no occurrence report written by a person is held, and the data
  a declaration or authorisation requires is not). Whether an obligation is satisfied is not
  said. The span is from the earliest dated record held to the computation date, against 3
  calendar years (a computation date of 29 February counts back to 28 February): it says how
  far back the records reach, never whether a record was ever removed, and an aircraft with no
  dated record has a span that is not known. The regulatory wording is paraphrased from the
  EUR-Lex consolidated text of 1 May 2025 (the wording of point (1)(g) there is the same as in
  the consolidation of 4 April 2022); a later amendment would not be noticed by the workbench,
  so a reader checks the consolidated text in force.
- **The journal head in the records summary is the store's last walk, not a walk per call.**
  The summary needs no token, so a caller may ask for it as often as they like. The store walks
  the journal once at startup and again only on the first snapshot after another connection
  has committed to the file; a write made through the store, once its commit has gone through,
  moves the kept head and length on by the link the store itself appended, with no row read,
  when the chain verified before it, and makes the next read walk again when it did not; a
  write whose commit is refused leaves the kept result where it was. The result a summary
  shows is the one labelled with the state the summary reads, never a newer or an older one:
  a summary that straddles a commit shows the head it began on, with the rows it began on, and
  walks nothing for it; the first summary after the commit walks once. Measured in the suite:
  twenty requests with no token, zero walks; one write through the store, zero walks and the
  head moved; one edit made in the file, one walk, and the break shown from then on; one
  outside commit landing inside an open snapshot, zero walks for that snapshot and one for the
  next. What the summary shows is exactly what `uasw verify` would compute of that state,
  including the 3.9 s a 100,000-link store costs the one walk after an outside commit, which
  lands on the first reader after it and on no write.
- **The fleet records summary is the per-aircraft summaries, read in one state, and nothing
  more.** `uasw fleet-records` and `GET /fleet/records` build each row with `build_records` and
  keep its statuses, span word and counts; a row has no reason or value its own summary does
  not have. The rows, the projection behind them and the one integrity line are read inside
  one snapshot on the request thread's own connection, which holds no write lock: a commit
  from another connection while the summary is built is not in it and is met by the next
  build, and a write through the store issued meanwhile lands at once and is in the next build
  too; measured in the suite, a write issued into a build held open finishes while the build
  is still held, and the build shows the state it began on. The summary costs the work of
  asking for each aircraft's summary in turn, in one request, and no walk of the journal
  (twenty fleet requests with no token, zero walks; one outside commit, one walk for the next
  build, not nine). At fleet level a record the store cannot read follows the board's liveness
  rule through the rows: one entry that cannot be read leaves maintenance activities and the
  span not known on every row, because liveness is decided over the whole ledger, and a flight
  record that cannot be read leaves only its aircraft's operations and span not known; the
  counts count such rows as not known and in no other bucket; the first unreadable record is
  named once; and the summary is written where `/aircraft` answers 503, because a record that
  cannot be read is part of what the records show. A count of not-known rows is a count, never
  a finding.

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
  runs were recorded again in 0.3.0 after the synthetic fleet changed, and two of those
  (which aircraft are not serviceable; what was superseded on SYN-01) again in 0.5.0 after
  it changed once more, each time with the same model and weights at the same computation
  date (CHANGELOG.md says which and why); the earlier files are in the history. A run is
  recorded once and kept as it came: the 0.5.0 answer to the first of those two says the
  remaining aircraft "have unknown maintenance status" where the 0.3.0 answer said "have no
  maintenance record entered", the same records phrased less exactly, and it stays. Between 0.2.0 and 0.3.0 one run, made before the
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
