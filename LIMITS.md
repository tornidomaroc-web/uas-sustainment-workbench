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
- **A ULog that pyulog reports damaged is refused, on pyulog 1.2.4 and 1.3.0.** pyulog skips a
  message it cannot parse and marks the file corrupt: a data message whose framing breaks, on
  both versions, and from 1.3.0 (PX4/pyulog#128) a format definition or a subscription too,
  where 1.2.4 raised and the log was refused in pyulog's words (a `KeyError` naming the
  subscription it could not match). The flight read from what is left can miss a topic, which
  then reads as one the log never held ("no vehicle_land_detected topic"), or most of its span,
  so such a log is refused, as a damaged DataFlash log is, with the same words on what that
  leaves out of the counts: "pyulog found messages it could not parse and skipped them;
  nothing from this log is stored, ...". Measured 2026-10-21 on 448 damaged copies of two logs
  (the PX4 fixture excerpt and the synthetic flown log of `tests/test_flight_px4.py`: cut short,
  one byte inverted, 64 bytes overwritten, definitions and flags crafted): before this check
  1.2.4 stored a flight for 322 copies, 37 of them marked corrupt, and 1.3.0 for 345, 62 marked
  corrupt (23 of those 1.2.4 refused); in the 58 marked corrupt that 1.3.0 stored and that were
  not crafted, the flight time or landings differed from the undamaged log's in 18 and the
  span in 13, down to 17.1 s of 204.3 s. With the check 1.2.4 stores 285 and 1.3.0 283, none
  marked corrupt, each record the same on both versions and as before. The two apart hold a
  definition declaring an array of more than 65 535 or fewer than 0 elements, which 1.3.0 marks
  corrupt and 1.2.4 reads, at a cost in time that grows with the size it declares: 33 s for a
  139-byte file declaring 10 million. None of the 45 real logs measured here (the fixture, the
  43 PX4 logs above and the whole log the fixture is cut from, 2.3 GB) is marked corrupt by
  either version; each gives the same
  record on both, and `uasw ingest` prints the same lines for it, as before the check. What
  pyulog does not see, the check does not: a value damaged inside a message that still frames
  (a ULog carries no checksum) reads as written, and a log cut short reads to the cut, as
  before. So does a log that ends in zero bytes after its last whole message, as a log still
  being written when the power went can: pyulog reads a message header of zeros there and
  marks the file corrupt, so the log is read again ending at that header, and reads to that
  message as 0.6.3 read it; it is refused only if pyulog finds damage before the zeros too.
  Measured 2026-10-21: each real log above but the 2.3 GB one (44), with 512 or 4096 zero
  bytes appended, reads to the record of the log without them on both versions, where the
  check alone refused all 88; with 512 bytes of 0xFF or random bytes appended, pyulog
  marks none corrupt and each reads as the log without them. On 218 other damaged copies of
  the same two logs (cut at 24 points, one byte inverted at 48, 64 bytes zeroed or randomised
  at 16 each, zero, 0xFF or random bytes appended), the check stores 130 on both versions and
  reading past a zero tail 135, each the same record on both versions and as before: the four
  with zeros appended read to the record of the log without them, and the one whose last 64
  bytes were zeroed reads to its last whole message, as a log cut there does.
- **A DataFlash file that does not frame is refused before pymavlink reads it, in one line.**
  pymavlink's indexer prints one `bad header` line on standard error for every byte it cannot
  frame, from compiled C in the wheels published for Linux and Windows, so no redirect of
  Python's stderr sees them. A `.bin` with no plausible format definition anywhere (a printable
  name, format characters pymavlink knows, printable labels) is refused before pymavlink opens
  it, whatever its size: 0 lines for 16 B to 16 MiB of zeros, text, every byte value or random
  bytes, measured 2026-10-04 through the reader, `POST /ingest` and `uasw ingest`, where 0.5.1
  wrote one line per byte (13 for 16 B, 16 776 689 for 16 MiB of zeros; fewer for random bytes,
  whose chance headers stop the indexer early). A file that holds a definition is then walked
  message by message, as pymavlink's indexer walks it, and refused at the first place the
  indexer would print a line: bytes that are not a message, a type no definition names, a
  definition shorter than the 3-byte header (issue #85). So a definition followed by 4 MiB of
  zeros is one refusal line, where 0.6.0 printed 4 193 778 lines in 15 s first, and noise before
  a log, or a log damaged inside, is refused naming the offset, where 0.6.0 printed a line per
  unframed byte and then stored a flight read from the next sync pair the indexer met, which can
  sit inside another message's data. The rule is the indexer's own, so there is no threshold to
  choose: in 800 damaged copies of two fixture logs (bytes overwritten, inserted, deleted, cut,
  added at either end, a type planted) the check refused exactly the 566 copies on which
  pymavlink printed anything, on 3.12 and 3.14, and every real log measured, the 3 fixture
  excerpts and the 10 whole ALFA logs, frames from its first byte to its last and reads to the
  same record as before. What it does not see: a damaged message body whose header is intact (a
  DataFlash log carries no checksum, so pymavlink reads the wrong values as it did), and bytes
  that do not frame in the last 527 bytes of a file of 528 bytes or more, which pymavlink
  allows for a block-logging page left partly written and skips a byte at a time without a
  word, as before; the walk skips them the same way. Every format definition the walk frames,
  in that tail too and after such bytes, is refused when its length is not 3 plus the size of
  its own format, or its format holds a character pymavlink
  has no size for (issue #88), naming its offset: "the format definition at offset N gives a
  length of L bytes, where its format needs M". Such a file frames, and pymavlink's parser
  printed a `Failed to parse` line on standard error and an `unpack requires` line on standard
  output for each message of that type it read, then read on (measured on 0.6.1: 19 997 lines
  on each for 2 MB of such messages of a type the tool reads, interleaved with others, and the
  log stored; about 990 on each, then a 422, when they run back to back); an unknown format
  character was already a 422, after one line on standard output. The definition is refused
  even when no message of its type follows and pymavlink would say nothing: a length the
  format does not give is not what a logger writes, and all 1 413 definitions in the 13 real
  logs give exactly 3 plus their format's size. In 607 copies of the fixture logs with
  definitions' lengths or format characters changed, misfit definitions planted before and
  inside the tail (after noise there too), some cut short or padded, the check refused all
  454 on which the installed pymavlink printed anything or raised, and 46 more, each holding
  a misfit definition of which pymavlink parsed no message; it passed the other 107, on 3.12
  and 3.14. A log
  stored before this check that holds such a definition stays stored and counted, and its
  store verifies as before; nothing reads the file again, but sending it again is now this
  refusal (`POST /ingest` 422, `uasw ingest` exit 1), where 0.6.1 answered 409 "already
  stored" to `POST /ingest` and `uasw ingest` logged that it was already stored and exited 0.
  A log cut short inside a message still reads. The walk is one more pass over the file,
  about 4 ns a byte here (0.33 s for the 80 MB ALFA log, whose read went from 0.25 s to
  0.58 s; checking its 140 definitions adds nothing measurable), with no list of offsets kept
  and no memory beyond the mapped file. Its worst case is a file of nothing but definitions,
  each with a 16-character format: 6.4 to 6.7 s at the 512 MiB limit, about 12 ns a byte,
  where the walk before this check took 1.7 s.
  A refused log may be a real flight, and nothing of it is counted, so items read less due
  than they are; the refusal says so. Its flight adds no cycle, and its hours come back only
  when a later log of the same aircraft carries the lifetime counter (`reconcile()`, to within
  one 30 s flush). On the three fixture logs of one aircraft (2 322.8 s, 3 cycles), refusing
  the middle one leaves 2 314.4 s and 2 cycles, and refusing the last leaves 572.4 s and 2
  cycles until a later log arrives. No entry records such a flight's usage by hand yet (issue
  #87). The rule is checked against the pymavlink installed, which the dependency allows to be
  any 2.x: a release that frames differently fails `tests/test_dataflash_framing.py`.
  `POST /ingest` takes the write token or loopback, so the file is an operator's own or a
  trusted writer's.
- **A log over 512 MiB is refused, and a log under it is still read whole by its reader
  (issue #89).** `POST /ingest` and `uasw ingest` refuse a log over the limit (413, the same
  sentence on both) before any reader opens it; `UASW_MAX_LOG_MIB` sets another whole number
  of MiB, for the service when it starts and for each command. The default is twice the
  largest real log read here (246 MB of PX4 ULog; the largest ALFA DataFlash log is 80 MB).
  The upload itself is copied a MiB at a time and no longer adds its size to the service's
  memory, but reading a log still costs memory by its size, on the command line as through
  the service: pymavlink's index of a DataFlash log is about 1.5 bytes per byte, so a
  DataFlash log at the limit can take some 0.8 GB while it is read, and uploads read at the
  same time add up. A lower limit is the only bound on that. Measured on 2026-10-15 against a
  live `uasw serve` on loopback (Windows, Python 3.12, rise in the process's peak private
  memory, main before the change against this change): 200 MiB of zeros named `.bin`,
  refused (422) as no log in about 0.9 s, rose 211 MB before and 4 MB now, two at once
  422 MB and 6 MB; the 80 MB ALFA log, accepted (201) in 0.9 s, rose 122 MB before and
  121 MB now, two different ones at once 210 MB and 207 MB, which is the reader's index, not
  the upload; 600 MiB declaring its length was read whole and refused (422) in 2.9 s, rising
  630 MB, and is now refused (413) in 0.3 s without its body being read. A body that declares
  no length is counted as it arrives and refused once it passes the limit and 1 MiB for the
  form's other fields, so up to 513 MiB of it is spooled to disk before the 413 (2.0 s for
  600 MiB here), then deleted. One that declares less than it sends is cut at what it declares
  by the HTTP server, and the bytes past that length are the next request on the connection,
  answered on their own (issue #99). A refusal answered from the headers alone (413 on the
  declared length, 401 or 403 without the token) leaves the connection open: the HTTP server
  reads and drops the body the client goes on sending, none of it reaching the service, and
  the next request on the connection is answered. The service does not close a connection on
  a refusal: a connection closed with bytes unread is reset, and on Windows a reset discards
  what the client has not read yet, the refusal with it (measured here with 2 MiB unread at
  the close: the client read nothing in 42 of 50 tries, on Python 3.12 and 3.14 alike). The
  disk holds an accepted upload twice while it is read (Starlette's
  spooled copy and the reader's own file). Every temporary file was gone after each request,
  refused, accepted, cut off by the client or failing to copy (a copy that fails answers 503);
  a service terminated while a reader has the file open leaves that one file behind (by
  construction, not measured). An upload, or an entry, without the write token is now
  refused (401 or 403) before its body is read; before, FastAPI read the whole form,
  spooling the file to disk, or the whole JSON body into memory (300 MiB to `POST /entries`
  raised the peak about 950 MB), before the write check ran. A writer's own entry has no
  size limit: FastAPI still reads it whole into memory.

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
  - *Pages of the file the walk does not read.* The walk reads the journaled rows, so damage
    elsewhere in the file (the `aircraft` page, an automatic index, `sqlite_sequence`) left the
    chain intact: 0.6.2 reported `journal intact: 94 links` on a seeded store whose `aircraft`
    page was overwritten at rest (issue #105). So `uasw verify` first runs SQLite's
    `PRAGMA integrity_check` on the whole file, and a file that fails it is one
    `refused (503)` line naming SQLite's finding, exit 1, with no walk and no `intact` or
    `unchanged` line. Measured on the seeded store with each b-tree's pages overwritten in turn
    (`aircraft`, `flights`, `entries`, `journal`, `sqlite_sequence`, each of the three
    automatic indexes) and with one key of the flights index edited in place: each is refused.
    The check is SQLite's, of the file's structure and of every index against its rows; a row
    rewritten in a well-formed page is the walk's to find, and a change to an aircraft row is
    found by neither. `GET /journal/verify` runs it too, before its walk, on a connection of its
    own, and a file that fails it is refused (503) on every route until the file passes it
    again (issue #107): a good copy restored with the service stopped is served when it starts
    again, and one restored in place is checked again at the first request after it, or
    within the wait below when one `stat` cannot tell it from the damaged file (below);
    `uasw serve` also runs it when it starts and on a file changed under it (below).
  - *Before the migration.* A store written by 0.5.x is journaled when this version first opens
    it, each row linked as found after a note that says so, with the counts and the time; a
    change made to it before that is linked as found.

  Only a head kept outside the file tells the first two from an untouched store:
  `uasw verify --head H` reports whether H is in the chain, which a truncated or recomputed
  chain fails, and nothing in this tool keeps a head anywhere. Writing the head down after each
  session, where the file cannot reach it, is the operator's, and so is deciding who may hold
  the file. The line is "unchanged since head H", and that is all it says: a false statement
  linked at entry is linked false, and what the ledger refuses or accepts is unchanged. Whatever
  a journaled row holds, verifying reports and never raises: a record that cannot be parsed or hashed
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
    nothing wait: it is the costliest read the service has, every row on every call, and since
    issue #107 SQLite's `integrity_check` on the whole file before the walk (measured through
    `uasw serve`, medians: 5.6 ms against 4.6 ms before on the seeded store, 203 against 155 ms
    at 9.4 MiB, 1.71 against 1.47 s at 83 MiB).
  - *One writer at a time, as before.* A second writer (`uasw record` beside a running service)
    waits up to 5 s (`PRAGMA busy_timeout`) for the write lock, then is refused with 503
    (`StoreBusy`); a refused commit is rolled back and moves nothing kept. A ledger entry takes that
    lock before its check, so it is refused with 503 and nothing checked or written. SQLite's rollback journal
    had no wait at all, so that failure came at once.
  - *Durability is as it was.* `PRAGMA synchronous` is FULL, the WAL default: a journal link is
    on disk before the commit that wrote it returns. NORMAL would save one sync per commit and
    could lose the last commits to a power cut; the link is the record and is not traded.
  - *A reader on another connection sees a snapshot and makes the commit wait for nothing.*
    `PRAGMA data_version`, which the kept journal result and `/health` are labelled with, moves on a
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
    must be on one host; the volume is, and a network file system is not supported. Nor is a
    folder a file-sync client keeps in step with other machines or a cloud account: it can
    copy `fleet.sqlite` and its `-wal` file at different moments while a store is open, which
    is not a complete copy, and a store opened on another machine's copy shares no lock with
    this one. Keep the store on a local disk and copy it as README says.
  - *The mode is kept in the file.* A store opened once by this version stays in WAL when an
    earlier version opens it. 0.5.2 was checked: it reads such a store with the same answers
    and writes to it, but 0.5.x keeps no journal, so a row it writes has no link and from then
    on this version reports the journal broken at that row (`unjournaled`); the migration runs
    once, so the break stays. A store file marked read-only is verified as before, but its
    close cannot fold the WAL in, so both side files stay until a store opens it writable.
    Those side files marked read-only too stop every read (issue #112): SQLite then keeps its
    index of the `-wal`, which lives in the `-shm`, in the reading process's memory, and every
    read on any connection is a new state of the store (`PRAGMA data_version` moves on each,
    measured on SQLite 3.49.1 and 3.50.4), so no snapshot can stand. Every command that opens
    an existing store looks at the `-shm` before SQLite opens anything (it is opened for writing
    and closed, nothing written) and refuses one it cannot write in one `refused (503)` line
    naming it, and the `-wal` when that cannot be written either, exit 1, the three files byte
    for byte as they were and nothing made; before, the six commands that only read, `record`
    on such a store and `uasw serve` ended in a `RuntimeError` traceback after 1000 tries to pin
    one state. A `-wal` alone that cannot be written stops no read, and is not refused. Should a
    `-shm` become unwritable under a running store, a read that cannot stand is refused as
    SQLite's own refusals are (503 in the service, one line in a command) rather than raising.
    A directory the process cannot write to was not measured; SQLite needs to create the side
    files there to open a WAL-mode file at all.
- **Threads never read through one connection; each read is one state of the file, and so
  is each records summary and evidence pack, while the board's responses are not.** Every
  request thread read through the store's one SQLite connection with nothing serialising the reads, and Python's `sqlite3` keeps one
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
  `/components`) and the static export's `fleet.json` are built from several reads with no
  snapshot around them, so one can straddle a write that lands between two of its reads, as
  it always could; none of them carries a journal head or a hash that names one state, and
  each is rebuilt on the next request. The gauges behind `/metrics` are one snapshot at one
  time for every aircraft (issue #92), refreshed one at a time, so the last refresh set is
  the newest state a refresh read. The three answers the
  store keeps rather than computes on every call, the kept journal result behind the records
  summaries, what `/health` holds and the whole-ledger projection, are each labelled with
  the state they were computed from; a snapshot uses one only when its label is the
  snapshot's own, computes it on its own connection otherwise, and keeps it unless a newer
  state's answer is kept already. The store's own commit moves the three labels on with
  nothing computed when the kept answers described the state right before it, which the
  writing connection's `PRAGMA data_version` tells (it moves on another connection's commit
  and never on the store's own; read before the label the write starts from and again after
  the new label, it says whether any other commit landed in between; read after that label
  instead, a commit landing between the two reads was missed, and the kept answers were moved
  on over it: a journal length one short, or "unchanged" and every record reading after an
  edit that broke the store, `tests/test_snapshot.py` at each of those reads), and drops them
  otherwise. So a snapshot never
  waits for a write and a write never waits for a snapshot: no read takes the write lock,
  and the one walk of the journal and the one read of the whole store that follow a commit
  from outside (1.3 s at 128,718 entries, above) run on the reading thread that meets it,
  where before they ran on the writing connection under the write lock and a write waited
  for them. What a write does pay: three `PRAGMA data_version` reads around its transaction,
  two on the labelling connection and one on the writing one (five, three and two, when
  another connection committed since the store's last own write), each a C call that lets the
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
  by keeping the service's threads busy, on this version as on the one before. A summary
  reads the whole ledger once per snapshot and folds it once per date it asks for, so the
  fleet summary costs about a third of nine summaries asked for in turn (74 folds of 29 dates
  for the seeded fleet became 29), and cheaper reads do not spare the writes of such a caller:
  they loop faster. Measured on a Windows desktop (Python 3.12.10, 2026-10-11; 20 `POST
  /entries` 100 ms apart, medians; before the summary read the ledger once, in brackets): 22 ms
  alone; 794 ms with 8 readers looping over the fleet summary, which answered 504 requests
  meanwhile (471 ms, 72 answered); 3.36 s with 32 (1.62 s); with 8 readers asking once a
  second each, 25 ms (25 ms), 213 ms at the 90th percentile (341 ms). Nothing in the service
  limits a caller's rate, which is the reverse proxy's or the operator's to do. A
  `wal_checkpoint(TRUNCATE)`, which every `uasw` command that writes and every store the
  service opens runs on close, moves other connections' `data_version` as a commit does, so a
  `uasw record` run beside the service makes the service walk the journal and read the whole
  store once more on its next call. A command that only reads runs none and commits nothing
  (issue #107), so it does not move them. The reading connections are closed by `Store.close`,
  from the closing thread, after the threads that read are done: `uasw` closes its store when
  the command ends, `uasw serve` after the server has stopped; a read after the close is refused
  as a read on a closed connection is, and a closed store is one file, as before. A store in
  memory has one connection: a snapshot there holds the write lock for its length, so threads
  read one at a time and a write issued during a summary waits for it and lands after it.
- **A write through the service still reads the whole ledger three times.** Validation reads
  it, the gauges read and fold it once for the whole fleet, and the answer's whole-ledger
  projection, which the write made stale, is read and folded once more (and used by the next
  write's validation), so a write costs about the size of the ledger, no longer that times
  the number of aircraft (issue #92: each aircraft's due list read and folded it twice).
  Measured through `uasw serve` (Windows, Python 3.12.10, 2026-10-15; `POST /entries` one
  after another, seeded fleet of nine aircraft): 506 ms a write at 3,000 entries before, 94 ms
  now; 1.80 s at 10,000 before, 0.32 s now. The board's own responses (`/aircraft`,
  `/fleet/due` and the rest above) still read and fold the ledger twice per aircraft per
  request, as before (18 folds for `/aircraft` on the seeded fleet). A store in memory holds its one lock for the whole refresh, where it
  took it once per read before.
- **Two entries appended at once are checked one after the other, through one store or from
  two processes.** An entry is checked against the records and then written, and the check
  and the write hold the store's write lock together, so a second request served beside the
  first is checked against the records with the first in them: two corrections of one entry
  posted at once give one 201 and one 409. Before, both were checked against the records as
  they were before either wrote and both were accepted (60 of 60 such pairs through `uasw
  serve` on Windows, Python 3.12, 2026-10-11, and 45 of 60 on the version before). That lock
  is the process's own, so on a store file the check also runs inside SQLite's write
  transaction (`BEGIN IMMEDIATE`), which holds across processes (issue #65): a `uasw record`
  run beside a running service, or two `uasw` commands at once, wait for each other and the
  second is checked with the first in the records. Two `uasw record` processes correcting one
  entry at one instant: one recorded, the other `refused (409)`, in 200 of 200 rounds (Windows,
  Python 3.12.10 and 3.14.3, 2026-10-12, 50 rounds each with and without a 0.4 s pause between
  check and write), where the version before recorded both in 20 of 20 with the pause and 18
  of 20 without. Reads go on beside the check on their own connections and wait for neither
  lock. A writer that waits more than the 5 s busy timeout for another is refused with 503,
  `refused (503)` on the command line, before anything is checked or written, so send it
  again. Opening a store no longer takes the write lock once the store is journaled, so a
  `uasw` command starts while the service writes. A store in memory has one connection and
  keeps the process's lock alone. An ingest writes a new aircraft's row and its flight in one
  transaction, so a writer that takes the store meanwhile leaves both or neither; one that
  waits past the busy timeout is refused with 503 (`refused (503): <log>: ...` per log on the
  command line, the next log still tried), where it answered 500 and could leave the aircraft
  row with no flight (issue #69). The first open of a store 0.5.x left, which switches the file
  to WAL and journals it, waits for another connection's write lock as every write does; past
  the busy timeout it is refused with one `refused (503)` line and exit 1, `uasw serve`
  included, with the file as it was, where it failed at once with a traceback (SQLite does not
  apply the busy timeout to that switch, so the store retries it; issue #70). A seed, by
  `uasw seed` or by `uasw serve` on its first start, is one transaction that takes the write
  lock before it looks at the store: refused or failing partway it writes nothing, and it
  writes only to a store that holds nothing, so `uasw seed` on any other store is one
  `refused (409)` line naming what the store holds, and `uasw serve` leaves such a store as
  it is (issue #72; before, a seed was about a hundred commits, a seed refused partway left
  part of the fleet, a second seed ended in a traceback, and `uasw serve` seeded a store that
  held aircraft but no flight). The seed holds the lock about 10 ms where its commits took
  about 0.4 s on the machine measured. Two seeds of one new file at once seed it once.
  The commands that write make the store on first use (`seed`, `serve`, `ingest`, `record`,
  in which a component can be registered in a new store); the commands that only read
  (`verify`, `records`, `evidence`, `fleet-records`, `export-static`, `record history`) and
  `record retract`, which needs an entry, refuse a path with nothing at it in one
  `refused (404)` line and make no file or folder, where a mistyped `--db` made an empty
  store that `uasw verify` called intact (issue #73). A write command given a mistyped
  `--db` makes a store there only when its write is accepted, since that is how a store is
  made: `ingest` reads its logs before it opens the store and opens it for the first log
  that reads (so an ingest whose logs are all refused prints only their 422 lines: it does
  not report a store it could not open, 503, nor migrate a 0.5.x store; the first log that
  reads does both), and `record`, on a path with nothing at it, checks the entry against an
  empty store before it makes the file, so a refusal (a file that is not a log, a date that is not
  a date or is in the future, an aircraft, component or entry the store does not hold, a
  kind fleet.toml does not name) makes no store, side file or folder there, where it made
  an empty store that `uasw verify` called intact (issue #83). Nothing is removed to get
  there, so a refusal never deletes or alters a store another process made meanwhile. An
  accepted entry is checked again against the store it opens, as every append is; if
  another process made and wrote that store between the two checks, the second check is
  the one that counts. `seed` and `serve` make the store on a new path as before. `export-static` builds
  the demo around the seeded fleet's SYN-04 and refuses a store without it (`refused (409)`)
  instead of ending in a traceback; exporting a store of one's own logs is not supported.
  The default `--fixtures` is the folder beside the code when it exists, so a seed run from
  any working folder of a checkout, or of an editable install of one, includes the two
  showcase aircraft; `UASW_FIXTURES` still overrides it. A package installed from a wheel has
  no fixtures beside it: its seed falls back to `tests/fixtures` in the working folder and,
  when there is none, leaves the showcase out with a warning in the log (the container sets
  `UASW_FIXTURES`). A store path a command may not look at (a folder it cannot read) is not
  taken for a missing store: the open refuses it (`refused (503)`).
- **A store file damaged while `uasw serve` runs is refused, with nothing written.** SQLite
  does not see a file changed outside it: its connections go on serving the pages they hold.
  So the store looks at its file (one `stat`, about 6 µs here: which file, size, time of last
  change, permissions) before every read and every write, and when the file changed and SQLite
  did not make the change, it runs `PRAGMA integrity_check` on a connection of its own, which
  also finds an index key edited in place: `quick_check`, which it ran before issue #107, passed
  one, and the service answered `GET /journal/verify` ok and wrote an entry into that file. A
  file that fails it (overwritten, truncated, pages overwritten, an index key edited) is
  refused on every route that reads or writes the store, `/health` included, with 503 naming the cause; `/metrics`, which reads no
  store, still answers. While the check runs, every other read and write waits for its
  verdict. A file restored in place is checked again and, sound, served: every
  connection is opened afresh, so no page read from the damaged file is served, and every kept
  answer is computed again (it carries the connection generation in its label). That holds for
  a restore made while the service's `-wal` file holds no writes of its own: SQLite reads the
  pages that file holds over whatever the store file now holds, so a copy restored while the
  service has written since its last checkpoint is overlaid by those writes (measured: the
  service went on answering its own last state, and the next checkpoint wrote it over the
  copy, whose own entry was lost). Restore a store with the service stopped. A file
  replaced or removed while the service runs is refused until the service is restarted, since
  SQLite's side files belong to the file that was there (Windows refuses to replace or remove
  a file the service holds open). A store made read-only is still read; its writes are refused
  with 503. Anything else SQLite refuses on the file is a 503 with SQLite's sentence, never a
  500. What this does not catch: a change landing within the same clock tick (about 15 ms on
  Windows) as the change last seen, at the same size, is seen only at the next change, which
  may be the store's own next checkpoint, or at the next `GET /journal/verify`, which runs the
  check whatever the look says and has the file refused from then on when it fails; a damaged
  file is looked at again regardless after a wait that grows with its last check's time
  (below), so such a restore is seen within that wait and one check. Nor damage to a page the `-wal` file holds a newer
  copy of: SQLite reads that page from the `-wal` file, so the check passes, and the next
  checkpoint writes the page over the damage (measured on 0.6.1 as on this version: pages
  overwritten that the `-wal` file held passed the check; any page it did not hold failed it).
  The store makes the only changes it makes to its file while open itself (issue #79): its
  writing connection never checkpoints on its own (`wal_autocheckpoint` is 0), and the store
  folds the WAL into the file every 200 of its own commits and after each batch, with
  `PRAGMA wal_checkpoint(RESTART)`, which waits, up to the busy timeout, for the reads already
  under way on a state older than its last commit to end (reads begun meanwhile are not waited
  for), so the whole WAL is folded and the next commit starts it over. One that waits for no
  reader (`PASSIVE`) folds it whole only at a moment no read is open, which threads reading
  without pause rarely leave, and the `-wal` file then grew with every write, without bound
  (84 MiB after 6,000 writes beside two such threads). The file is looked at right
  before and right after it, under the lock every look takes, and checked once, by the
  writer, when the two looks differ and whenever the checkpoint had own commits to fold,
  whether or not they differ: the checkpoint's write stamps the file with the system clock,
  whose tick is about 15 ms on Windows, and one that writes no page past the end of the file
  within the tick of the change last seen leaves both looks equal (issue #95: measured on
  Windows, the two looks were 12 to 66 ms apart at the same size; with no check run then,
  after damage landing during such a checkpoint the service answered reads and writes 503
  with SQLite's own "database disk image is malformed" rather than the 503 naming the cause,
  and wrote no entry; 0.6.1, whose own checkpoints SQLite made on its commits, did the same).
  So damage landing
  during the checkpoint is refused, and a file that passes is recorded as seen with nothing
  else done: no connection is opened afresh and no kept answer is computed again. Every
  checkpoint the store runs has own commits to fold, since it runs one every 200 of them and
  after each committed batch, so every one is followed by the check (`_checkpoint` still
  carries a guard for a checkpoint with nothing to fold, which no store on a file reaches).
  So a change from outside that lands during the
  store's own checkpoint and passes the check (a copy restored in place) is served without the
  connections opened afresh; restore a store with the service stopped, as above. Before, SQLite
  checkpointed on the commit that took the WAL past about 1,000 pages, and on nearly every
  commit while readers kept the WAL from starting over, and each such checkpoint looked like a
  change from outside. Measured on Windows (Python 3.12, 2026-10-14; 6,000 writes through one
  store, two threads each reading in snapshots, without pause or holding each for 20 ms): 30
  checks, every checkpoint folding the whole WAL, and no connection reopened, where 0.6.1 made
  113 and 227 checks, each followed by every connection opened afresh and the kept answers
  computed again; the two threads read 2.5 to 2.9 times as often, and the `-wal` file stayed at
  3.7 MiB where 0.6.1 left it at 6.0 and 19 MiB. A legitimate change from outside
  (another `uasw` command's commit and the checkpoint its close runs) is still one: one
  `integrity_check` and the connections opened afresh, besides the journal walk its commit costs
  anyway (ten `uasw record` processes beside one store: ten checks and ten reopenings, on 0.6.1
  and on this version), since one `stat` cannot tell another connection's checkpoint from a
  file restored in place. The check is about 3 ms per MiB of store here: on a connection of its
  own, 0.9 ms on the seeded store, 28 ms at 9.4 MiB and 257 ms at 83 MiB, where `quick_check`
  took 0.8, 27 and 230 ms (Windows, Python 3.12.10 with SQLite 3.49.1, 2026-10-18, medians of
  15); through `uasw serve`, the first `/health` after the file's time of last change moved
  took 112 ms at 9.4 MiB and 1.0 s at 83 MiB, 13 and 41 ms more than with `quick_check`, the
  rest being the walk and the whole-store read every change from outside costs. While it
  runs, every other read and write that meets the change waits for it, the store's own
  checkpoint included, since a read let through meanwhile could read a damaged file. On a
  store whose check takes seconds that wait is the check's own time: on the 113.7 MiB store of
  795,000 rows indexed in random order in the at-rest bullet below (a 6.3 s check), one
  `/health` sent while a `uasw record` ran beside `uasw serve` took 6.0 s, and the service's
  own checkpoint held its
  200th write 6.1 s, where 0.6.2, which ran `quick_check` (0.24 s there), took 0.26 and 0.3 s
  (Windows, Python 3.12.10, 2026-10-20, `/health` sent 20 times a second). A damaged
  store whose file looks as last seen is checked again only after a wait timed from the end of
  its last check: nine times as long as that check took, and never less than a second (issue
  #113), however many requests meet it. So its checks take at most a tenth of one core, where
  a wait of one second let them take check / (check + 1 s) of one. The bound this sets: damage
  is found at the first request after the file's `stat` changes, as before, and so is a good
  copy restored in place or a restart with a good copy; only a restore that one `stat` cannot
  tell from the damaged file (same size, same time of last change) waits, and is served within
  max(1 s, 9 × the check's time) plus one check. Measured through `uasw serve` (Windows,
  Python 3.12.10 run directly, 2026-10-19; damage that SQLite finds only at the end of its
  check, one index key edited, so each check takes its full time; share of one core over 30 s,
  request handling included):

  | Store | Check | `/health` 20/s | 100/s | No request | Restore seen (same `stat`) |
  |---|---|---|---|---|---|
  | 82 MiB of 1 KB rows, before | 0.16 s | 16 % | 23 % | 0 % | 0.6 s |
  | the same, now | 0.16 s | 13 % | 17 % | 0.3 % | 1.0 s |
  | 86 MiB of 795,000 indexed rows of 60 bytes, before | 4.0 s | 81 % | 85 % | 0 % | 5.7 s |
  | the same, now | 4.0 s | 14 % | 18 % | 0.2 % | 34.6 s |

  With no damage, 20 requests a second took 2 to 3 % on either version. Unchanged by it: the
  damage was answered 503 within 0.2 s and 3.4 to 3.6 s (the first check's time), a restore in
  place that moved the file's time of last change was served within 0.2 s and 4.2 to 4.5 s,
  and a service stopped, given a good copy and started again answered `/health` in 1.5 s and
  9.3 to 9.4 s, the at-rest check included. Damage found early in the check makes each check
  short: 64 pages overwritten mid-file were found in 0.03 s on both stores, and the service
  took 5 to 12 % on either version. The check's time follows the rows and index keys more than
  the bytes (`integrity_check` took 0.16 s on the first store and 4.5 s on the second, where
  `quick_check` took 0.2 s on both), so the 3 ms per MiB above holds for rows the size of this
  store's own. The checkpoint's
  wait for an older read holds the store's write lock, so the write that runs it, and every
  write through the service sent meanwhile, answer only when that read ends or the busy
  timeout passes; then the checkpoint is left for the next one and the writes are answered
  as usual. Measured through `uasw serve` (Windows, Python 3.12.10, 2026-10-15; a reader in
  another process begun before the 200th write): held 3 s, the 200th write and one sent
  0.3 s after it answered 201 in 3.4 s and 3.1 s; held 8 s, in 5.6 s and 5.3 s; `/health`,
  `/aircraft` and `/metrics` sent meanwhile answered in 10 to 70 ms, and a `uasw record` run
  meanwhile recorded its entry; no 500, every write in the ledger and the journal intact,
  on this version as on the one before.
  Damage to the `-wal` or `-shm` side files is not looked for. `/metrics` gauges describe the
  state the service read after its last write or ingest; a commit from outside is in them
  from the service's next write on.
- **A store file damaged at rest is refused by every command that writes, and by
  `uasw verify`.** A write's own reads touch only some pages, so 0.6.2's `uasw record` wrote
  and exited 0 on a seeded store whose `flights` pages were overwritten while no process held
  it, and `uasw ingest` did the same with only the `entries` pages or only `sqlite_sequence`
  overwritten; every later `verify` then refused the file, or, for `sqlite_sequence`, called it
  intact (issues #104, #105). Now every command runs SQLite's `PRAGMA integrity_check` on an
  existing store file before the store is opened on it: `seed`, `serve`, `ingest`, `record`
  and `record retract`, `verify` before its walk, and the commands that only read (`records`,
  `evidence`, `fleet-records`, `export-static`, `record history`, issue #107): a file that fails is one `refused (503)` line naming SQLite's
  finding, exit 1, the file byte for byte as it was. A `-wal` that a hard stop left beside it
  holding anything, its 32-byte header alone included, stays byte for byte as it was, with
  its `-shm` beside it, rather than folded into the damaged file and removed, as the check's
  connection did when it closed last (found on Windows by hand, a `uasw serve` stopped hard
  after three writes, then the `aircraft` pages overwritten; the check reads the pages held in
  the `-wal`, so an index key edited only there is refused too). The `-shm` is SQLite's index
  of the `-wal` and holds no record: the check's read rebuilds it, so its bytes change on
  every such refusal. An empty `-wal` is removed with its `-shm`; with none, no `-wal` or
  `-shm` is left, except beside a file marked read-only, where SQLite's side files stay as for
  any open of it (above).
  The kept `-wal` and `-shm` belong to the damaged store, and the refusal says so: the line
  then reads "move the file, its -wal and its -shm aside together, then put a good copy where
  the file was". Put in the file's place with that `-wal` still beside it, a good copy takes
  the `-wal`'s pages in when it is next opened: measured on Windows (issue #108), a good copy of
  61 entries and 95 links came back with 63 entries, its own entry gone, and `uasw verify`
  called it intact at 97 links. SQLite records nothing in the file that ties a `-wal` to it
  (the `-wal`'s header holds its own salts, and the good copy's header, its change counter,
  page count and schema cookie included, was the same as the damaged file's), so the store
  cannot tell a `-wal` written against another file from its own; moving the three aside
  together is the only protection. With a `-wal` kept, the file alone is
  checked as well (read through SQLite's `immutable` mode, which opens the file read-only with
  no locks and reads neither the `-wal` nor the `-shm`), and the line says where the
  damage is: "the store file fails SQLite's integrity check", or, when the file alone passes,
  "the store's -wal ... fails SQLite's integrity check ... where the file alone passes it, so
  the store is damaged in writes held only in the -wal". The file alone is then the store
  without those writes. The second check runs only on such a refusal and reads the file from
  disk, as the first does. It first read a copy of the file into memory, which took twice
  the file's size (2084 MiB more at 1033 MiB, measured on Windows) and, at 2 GiB or more,
  ended the command in a `MemoryError` traceback, past what SQLite allocates at once; read in
  place, it took 2 MiB more at 115 MiB, 1033 MiB and 2113 MiB alike, and `verify`'s refusal
  of the 2113 MiB store took 9.7 to 9.9 s, where it took 2.8 s without the second check.
  A hard stop during a checkpoint leaves the file part-written and the `-wal` whole: measured
  on Windows (2026-10-18), the store's own checkpoint after one batch of 186 MiB took 1.6 s,
  and stopped hard 40, 200 and 600 ms into it the file held 9.5, 49 and 119 of its 194 MiB.
  The store with its `-wal` then passes the check and the commands that only read answered
  (`journal intact: 120094 links`) with the file and the `-wal` as they were; the next write
  folded the `-wal` in and `verify` called the store intact at 120095 links. The file alone
  fails ("database disk image is malformed") until that fold, whatever is damaged, so the file
  alone cannot say where damage beside a fold cut short is (issue #111). A checkpoint that
  ends can leave the same: held back by a reader of an earlier state (SQLite's own PASSIVE
  checkpoint, or the store's RESTART when a reader outlasts its 5 s wait), it folds only the
  pages that state last wrote, and a page written again since keeps its older content in the
  file, so the file alone can fail with nothing damaged while the store with its `-wal`
  verifies unchanged (made by real SQLite in `tests/test_fold_cut_short.py`: a table made,
  then a row put in it, folds page 1 and not the table's root). When the file alone fails
  beside a `-wal` holding committed pages, the check looks at those pages in the file: some
  page in the file exactly as one of its copies in the `-wal`, and some page not in it as its
  latest copy, is the file holding part of what the `-wal` holds, and the line then says "the
  store fails SQLite's integrity check (...), and the file holds part of what its -wal holds,
  as a fold of the -wal into the file leaves it when cut short by a hard stop or held back by
  a reader of an earlier state, which can leave the file alone failing the check with nothing
  damaged, so whether the damage is in the file or in the -wal cannot be told". Every page in
  the file as its latest copy (a fold finished whose frames stayed), or none as any copy (no
  fold begun since the `-wal` last started over, which SQLite does only once all of it is
  folded), leaves the file alone a store of its own, and the line names the file as before; a
  file alone that passes still names the `-wal`. What the line says to do with the three files
  is unchanged.
  Measured on Windows (Python 3.12.10, SQLite 3.49.1, 2026-10-19): the seeded store, then 186
  rows of 1 MiB written in one transaction by a process stopped hard after its commit (file
  0.1 MiB, `-wal` 187.3 MiB, 47,680 pages), then `PRAGMA wal_checkpoint(FULL)` stopped hard
  300, 500, 700 and 900 ms in (the file held 14.2, 62.9, 101.5 and 160.3 MiB, 3,598 to 41,014
  of the pages copied), then one page the fold had not reached overwritten in the `-wal` with
  its checksums computed again: the code before issue #111 said "the store file fails" each time, this code
  says it cannot be told, with the file and the `-wal` byte for byte as they were; with no
  checkpoint begun, both name the `-wal`. The look reads frame headers and, on such a
  refusal only, each page the `-wal` holds from both files, its latest copy first and its
  older ones only until one page has been met in the file as a copy, stopping once it has met
  that and a page not as its latest copy: `verify`'s refusal took 0.8 to 1.0 s where it took
  0.6 to 0.7 s on the code before issue #111, its peak memory 74 to 75 MiB. With every page twice in a
  374.7 MiB `-wal`, nothing folded and the file damaged (the look reads every copy), it took
  1.5 s and 77 MiB, and named the file. It counts frames as SQLite does, by their salts up to
  the last commit, without computing their checksums; frames under older salts, left when the
  `-wal` started over, are not read. Where a page was written whole and its content happened
  to be the same as an earlier version, a fold not begun could read as part of one, and the
  line would say it cannot tell, which is true, rather than name the file; no such page was
  met in the shapes tried (a rebuilt index, three entries, the bulk write, the five held
  back). A copy damaged in the `-wal` after a fold wrote it into the file no longer matches
  the file, so where that was the only page folded, the file reads as no fold begun and the
  line names it, as it did before this look.
  `integrity_check` rather than SQLite's quicker `quick_check`: both found every b-tree's pages overwritten in turn on the seeded store, and
  only `integrity_check` found one key of the flights index edited in place. Measured on
  Windows (2026-10-18, a connection of its own, medians; Python 3.12 with SQLite 3.49.1, 3.14
  with 3.50.4): 0.7 to 0.9 ms on the seeded store (0.1 MiB), 25 to 27 ms at 9.7 MiB (85 times
  the seeded size), 244 to 266 ms at 84 MiB, where `quick_check` took 0.7 to 0.8, 22 to 23 and
  192 to 202 ms. Its time follows index keys stored out of their rows' order more than bytes:
  on a store of 113.7 MiB, the seeded store with a table of 795,000 rows of 60 bytes added,
  indexed on keys written in random order (Windows, Python 3.12.10, SQLite 3.49.1,
  2026-10-20), `integrity_check` took 6.3 s and `quick_check` 0.24 s; the same rows with keys
  written in order took 0.49 and 0.22 s (114.7 MiB), and the seeded store with 800,000 work
  orders appended through the store 1.48 and 1.20 s (537.9 MiB), its tables being indexed in
  the order rows are written apart from the flights' aircraft and log reference. Every
  command pays it as it opens the store: `record history` and `record` took 6.1 s each where
  0.6.2 took 0.39 and 0.45 s, and on a like store (113.8 MiB, a 5.7 s check) `serve` and
  `seed`, which on a store that holds data run it again at the checkpoint after their seed
  finds the store full (issue #117), took 12.0 and 11.7 s where 0.6.2 took 1.05 and 0.83 s
  (real processes, medians of three interleaved rounds).
  Through real processes (four interleaved rounds, medians) the commands'
  own run-to-run spread was larger than the check: on the seeded store `serve` reached its
  first `/health` in 1.0 s on this version and 1.3 s on 0.6.2, and `record`, `ingest` and
  `verify` took 0.4 s and 0.6 to 0.8 s; on the 9.7 MiB store `serve` started in 1.5 s and 2.1 s,
  and `record`, `ingest` and `verify` took 59 to 68 ms longer than on 0.6.2. Reads through a
  running service run it only after the file changed under it (the bullet above) and take no
  new lock: `/health`, `/aircraft`, `/fleet/due` and
  `/aircraft/SYN-04/due` answered within a millisecond of 0.6.2 on the seeded store. What it
  does not cover: a file damaged after the check, while the command runs, is left to the open
  store's look at its file, as in the bullet above; and a row rewritten inside a well-formed
  page passes it (the journal walk finds that in a journaled row).
  The commands that only read run it since issue #107: before, on a store with one key of the
  flights index edited in place, `records`, `evidence`, `fleet-records` and `export-static`
  wrote their output and `record history` printed its history, each exit 0. Measured (Windows,
  Python 3.12.10, 2026-10-18, real processes, five interleaved rounds, medians), `record
  history` took 394 ms against 403 ms before on the seeded store, 505 against 473 ms at
  9.4 MiB and 1.30 against 1.10 s at 83 MiB, the check's own time; `verify`, which ran it
  already, took the same. A command that only reads also leaves a `-wal` it finds holding
  anything as it was, whether it answers or refuses: it runs no checkpoint as it closes, and
  beside such a `-wal` it tells SQLite not to fold it in when its last connection closes
  (`SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`). Before, it folded a hard stop's `-wal` into the file
  and removed both side files, `record history` did so on a store the check refuses, even when
  it refused the command itself, and damage held only in the `-wal` was then in the file.
  Beside no `-wal`, or an empty one, a closed store is still one file after it. The checkpoint
  it ran as it closed also waited for another connection's write lock: with one held beside
  it, `verify`, `record history` and `records` took 5.9 to 6.1 s (the 5 s busy timeout, then
  the checkpoint given up), and take 0.4 to 0.6 s now; beside a running service, `uasw verify`
  truncated the service's `-wal` and moved its `data_version`, and now moves neither. SQLite's
  read-only (`mode=ro`) and `immutable` opens were measured as the other way to keep the
  `-wal`: read-only left an empty `-wal` and a `-shm` beside a closed store, and `immutable`
  reads the file alone, 60 of a hard-stopped store's 63 entries; neither checks anything.
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
  including the 2.4 s a 100,000-link store costs the one walk after an outside commit, which
  lands on the first reader after it and on no write.
- **The fleet records summary is the per-aircraft summaries, read in one state, and nothing
  more.** `uasw fleet-records` and `GET /fleet/records` build each row with `build_records` and
  keep its statuses, span word and counts; a row has no reason or value its own summary does
  not have. The rows, the projection behind them and the one integrity line are read inside
  one snapshot on the request thread's own connection, which holds no write lock: a commit
  from another connection while the summary is built is not in it and is met by the next
  build, and a write through the store issued meanwhile lands at once and is in the next build
  too; measured in the suite, a write issued into a build held open finishes while the build
  is still held, and the build shows the state it began on. The summary costs one read of
  the ledger and one fold per date inside its snapshot, where asking for each aircraft's
  summary in turn reads and folds it per aircraft, and no walk of the journal
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
