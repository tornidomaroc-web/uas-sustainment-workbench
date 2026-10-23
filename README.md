# UAS Sustainment Workbench

[![ci](https://github.com/tornidomaroc-web/uas-sustainment-workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/tornidomaroc-web/uas-sustainment-workbench/actions/workflows/ci.yml)

A tool you run on your own machine to keep the maintenance and operations records of a small
civil unmanned aircraft fleet. It reads the flight logs, counts what each aircraft and each
part has used, and says what is due and why.

**Live demo, synthetic data, nothing to install:**
<https://tornidomaroc-web.github.io/uas-sustainment-workbench/>

## In one minute

**What it does.**

- It reads flight logs in the ULog (`.ulg`) format of PX4 and the DataFlash (`.bin`) format
  of ArduPilot. Each log becomes one flight record: flight time, arm cycles, landings, battery
  energy and the faults the autopilot reported. A value a log cannot give is marked not
  known, with the reason.
- It keeps a maintenance ledger of work orders, components and inspections. An entry, once
  written, is never changed or removed. A correction is a new entry that supersedes the old
  one, which stays in the history.
- From the logs and the ledger it computes a due list and a board state for each aircraft:
  serviceable, serviceable with deferred defects, unserviceable, in maintenance, AOG, or not
  known. Every state but serviceable comes with its reason in a sentence.
- It writes a "Draft records summary for the operator's own review" for one aircraft or for
  the whole fleet.

**Who it is for.** An operator who must keep maintenance and operations records. One example
is UAS.SPEC.050(1)(g) of Regulation (EU) 2019/947. It asks the operator to keep records of
(ii) the maintenance activities on the aircraft and (iii) information on its operations,
including unusual occurrences, each for at least 3 years. The summary sets what the workbench
holds against those points. It holds no (g)(i) training records, and the summary says so.
For (iii) it holds the flight records and what they show as unusual, but no occurrence
report written by a person, so that point reads partly at most. The code is also here for
open-source maintainers to read and reuse, and for anyone reviewing how it was built.

**How it works.**

- Board states are computed from the logs and the ledger on every read. None is typed in or
  stored.
- It compares consecutive logs of one aircraft with the autopilot's own lifetime counter and
  reports flight that no log covers. On a public research dataset it finds "355 s of flight
  on aircraft alfa-fixed-wing is not covered by any log".
- Every entry and flight record is linked into a hash chain as it is written. `uasw verify`
  says whether the chain is intact. Given a head you kept from an earlier run, it says
  whether the records are unchanged since then. It never says they are true.
- The summary marks each point with one of four statuses: supported by these records, partly,
  not evidenced by this workbench, or not known. It decides nothing. The authority decides.
- Every aircraft, flight, component and work order is marked synthetic or real. The seven
  synthetic aircraft of the demo fleet describe no real aircraft.

**Try it in a few minutes.** You need git and Python 3.12 or later. Run these from the
clone's folder; the store goes to `data/local/fleet.sqlite`, which git ignores.

```bash
git clone https://github.com/tornidomaroc-web/uas-sustainment-workbench
cd uas-sustainment-workbench
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
python -m pip install -e .
uasw seed                                 # the demo fleet: 7 synthetic aircraft, 2 from public logs
uasw records SYN-04 --out records.html    # a draft records summary; open it in a browser
uasw verify                               # the write journal: intact, and its head
uasw serve                                # the HTTP API; then open http://localhost:8000/docs
```

**Released and unreleased.** The latest release is 0.6.4. Main can also hold changes made
after it, listed under Unreleased in [CHANGELOG.md](CHANGELOG.md). A clone gives you main;
`git checkout v0.6.4` gives you the release.

**What it does not do, and how it is checked.** It does not certify airworthiness or release
an aircraft to service. It takes each entry at the word of the person who made it: there are
no users, no roles and no signatures, and a write needs one shared token or, when none is
set, must come from this machine. What each part cannot tell, measured, is in
[LIMITS.md](LIMITS.md). On every pull request CI runs lint, strict types and the tests on
Linux and Windows, and renders the demo page in a headless browser. The code is written with
an AI coding assistant, as the Co-Authored-By lines in the commit history show. No pull
request carries a GitHub review. Pull request #110 carries a check posted before the merge
under the owner's account, written by a separate session of the same AI assistant, not by a
person: it ran the suite again and measured main against the change
(<https://github.com/tornidomaroc-web/uas-sustainment-workbench/pull/110#issuecomment-6256552924>).
Pull requests before it carry no such comment.

The rest of this README describes each part in detail.

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

**`FlightRecord` and `reconcile()`.** One typed record per log (aircraft key, UTC start,
flight time, arm cycles, landings, battery energy, fault events, the autopilot's lifetime
counter), where every value the log cannot support is an explicit `Unknown` with its reason.
`reconcile()` compares consecutive logs of one aircraft against that counter and reports
flight no log covers: on the ALFA excerpts, "355 s of flight on aircraft alfa-fixed-wing is
not covered by any log". Measured limits are in [LIMITS.md](LIMITS.md).

**Component life and the readiness board.** A limit may be set in flight hours, in cycles
and in calendar months, and whichever comes first applies (EASA MoC to OSO #3). An inspection
with a tolerance may be overflown by that much, and the overflown hours count toward the next
interval (14 CFR 91.409(b)); an annual inspection starts the 100-hour interval too ("an annual
or 100-hour inspection"); calendar months run to the end of the month (91.409(a)). Before an
aircraft's first log, when hours before it were entered as a total, its time in service is
not known, and so are the inspection items and the board that depend on it: the tool says
so rather than a state that reads as leave to fly. A
component carries its own usage and the airframes it was fitted to, so its life status moves
with it between airframes (14 CFR 43.10). Usage is the logged flight time plus the flight
`reconcile()` found no log covers, minus duplicate uploads. A flight's cycle and hours count
once the flight is over: a record holds no landing time, so a log counts from its end, the
last instant its flight can have ended, never from take-off. A limit counted in completed
units is reached when its last unit completes: a part at 300 of 300 cycles, or at the whole
of its hours, has nothing left for another flight and grounds the aircraft, with a sentence
that says it "has reached" its limit; a calendar life is valid through its last day and past
from the next. The ledger refuses to fit a part at the same points, so the board and the
write rules agree at the limit on every basis. A log is evidence and is never refused, so a
flight is counted whatever the board said; what the tool does is report it. Every log's start
is compared with the board state the records give at that instant, and a flight that began
while that state was a known unserviceable, in maintenance or AOG is reported in one
sentence naming the aircraft, the log's start, the state and its cause
([`life/grounded.py`](src/uas_workbench/life/grounded.py)). The sentence says that the
records and the log disagree, not which is right; a state that is not known is never a
finding, and such a log is listed as not judged, with why. From the due list the board state
is derived in civil vocabulary, worst first: AOG and in maintenance from open work orders,
unserviceable from any life limit reached or any limit past its tolerance, not known when an
item depends on a time in
service the records cannot give, serviceable with deferred defects from a limit inside its
tolerance or a deferred defect, else serviceable. Every limit and interval
lives in [`fleet.toml`](src/uas_workbench/fleet/fleet.toml) with its public source next to it,
as an editable default. Part 107 sets no inspection interval for small unmanned aircraft, so
the two inspections borrow the shape of the manned Part 91 rules and say so.

**A service you can run.** `docker compose up` starts an HTTP API over a SQLite store seeded
with a synthetic fleet of seven aircraft plus the two real showcase aircraft. `/docs` is the
auto-generated API reference; `/aircraft`, `/aircraft/{key}/flights` and
`/aircraft/{key}/reconcile` return the records and findings; `/aircraft/{key}/due` is the due
list with the board state and its reasons, computed now or at `?as_of=`, and `/aircraft` takes
the same `?as_of=`. Both, and `/fleet/due`, take `?cues=true`, which adds a short cue beside
every sentence: at most 40 characters, built from the same fields by one grammar in
[`life/cue.py`](src/uas_workbench/life/cue.py) ("BAT-04A reached 300-cycle limit",
"BAT-04B past calendar limit 2026-08-31", "100 h insp 3.6 h overdue, in tolerance", "100 h insp: time in service not known", "AOG
awaiting parts since 2026-08-14"), for a wall board that cannot carry a paragraph. A cue keeps
the part or inspection, the number, the unit and the direction, and says exactly what its
state says; the sentence stays as it is, and without the option the response is byte for byte
what it was. A state at a time is built from the ledger entries that had occurred by
then: liveness (what a correction or retraction has killed) is decided over the whole ledger
first, as known now, and only live entries dated at or before `as_of` count, together with the
flights whose logs had ended by then. `/fleet/due` is what
a maintenance lead asks first, everything due soon or overdue, worst first;
`/fleet/grounded-flights` and `/aircraft/{key}/grounded-flights` list the flights logged while
the records show the aircraft grounded, with how many logs were judged and which were not and
why, and take the same `?as_of=` and `?cues=true` ("flown in maintenance 2026-03-10 10:00"
on a hand-built test aircraft, beside the cues of its cause; the demo fleet has none). The
board, the due list and reconciliation do not carry these: the board is the state now and
its reasons, and a past flight is neither. The evidence pack
lists them in its usage section and the demo page above the fleet table; `/ingest` parses
an uploaded log and keeps no raw copy, and refuses one over 512 MiB with 413 before reading
it, as `uasw ingest` does (`UASW_MAX_LOG_MIB` sets the limit); `/metrics` is Prometheus
text and every request is one
JSON log line with a request id. The live demo above is the same data, generated by CI from
the seeded fleet on every merge, and a headless-browser check renders it on every pull
request, including the due view and the reasons under every non-serviceable state.

Every aircraft, flight, finding, component and work order carries `synthetic: true` or
`false`. The synthetic fleet is generated from a seed (`fleet.toml`) and describes no real
aircraft; its maintenance records are generated with it, fixed so that every civil board
state appears once, and its board states are computed from those records, never stored. It
exists so `reconcile()` has consecutive logs to compare and the life engine has records to
work from, and every record it writes is one the ledger's own write rules accept, in order.
A synthetic aircraft does not fly while its own records show it grounded: the generator
draws every flight, then leaves out any log that would start while its aircraft is
unserviceable, in maintenance or AOG, judged by the same comparison the service reports with.
Its batteries tell one story: a pack flies its 300th and last permitted cycle on SYN-01 on
2026-08-06, grounds that aircraft from the end of that flight's log, and comes off a week
later tagged unserviceable and segregated, never to be fitted again; a pack lent by SYN-01 to
SYN-04 carries its cycles with it; and SYN-04 is grounded from September 2026 by a pack whose
calendar life ended while it was fitted. SYN-05 does not fly after the work order on it
opens. The two real aircraft have no maintenance record, so their state is unknown, with
that reason.

This was not always so, and the released 0.4.0 shows it. Its seed kept every flight it drew,
so it recorded one take-off on a pack already at its limit, which that version's board
called serviceable, and six flights by aircraft its own board showed as unserviceable or in
maintenance: two on SYN-01 past that limit, four on SYN-05 with a work order open that said
to inspect before the next flight. The pack then went "into storage" at 303 cycles. 0.5.0
corrected the rules first, then reported those flights, then changed the generation; the
finding stays in the tool and reports any such flight in an operator's own records
(CHANGELOG.md, 0.5.0).

**An assistant that phrases, and never computes.** `uasw ask "what is due on SYN-04 before
2026-10-03, and why?"` sends the question to a small open model served locally by Ollama and
lets it call six of the service's read-only endpoints, one tool per endpoint, nothing else. The
computation date is pinned by the caller, and the model cannot move it. Every answer carries
the calls it made with the records returned, and a deterministic grounding check marks any
answer unverified that states a number, a component or aircraft id, or a date that none of
those records contain. The model is told that hours and cycles advance only with flight,
which cannot be predicted, and that the workbench does not certify airworthiness. Five runs,
recorded with `--record`, ship with the package; two of them were recorded again in 0.3.0,
and one of those again in 0.5.0, with the same model weights, each time after the synthetic
fleet changed (CHANGELOG.md says which and why). The live demo shows them under
"recorded runs, not live" with the model tag, the digest of its weights, the recording date,
the fixed computation date, and every record the answer rests on. A test replays each
recording against the current code on every CI run, so a change to the engine fails CI
instead of leaving a stale answer on the page. No model runs in CI or on the page. A tool
call the service refuses, a 503 naming a record the store cannot read or a 404 for a key
the model made up, ends the run before the model is asked again: `uasw ask` prints the
service's own refusal line and exits 1, and no answer is grounded on a read that failed.
A service that does not answer at all, or a model backend that answers an error, is not
running or does not hold the model, ends the run the same way, with the URL and the reason
on one `refused` line; never a traceback.

**A maintenance ledger that is only ever appended.** Everything an operator records goes in
as an entry: what happened, when it happened, when it was entered, by whom, and in what
words (14 CFR 43.9(a): a description of the work, its date, the name of the person). Work
orders open, change state and close, and drive the "in maintenance", "AOG" and "deferred
defects" states; components are registered, installed and removed, and their counters
travel with them (14 CFR 43.10); inspections are recorded as done, at the aircraft's time in
service, which the tool derives from the logs if the person does not state it; the hours
before the first log are set. Entries are never edited or deleted. A correction is a new
entry that supersedes an older one with a reason, a retraction supersedes without
replacing, and undoing a correction revives what it corrected. The records the life engine
reads are projected from the live entries on every read, so nothing about maintenance is
stored twice. Impossible input is refused before anything is written, with a status and a
sentence: an unknown aircraft or component, the two public showcase aircraft, a work order
closed that is not open, a component installed while it is on another airframe, a part
fitted after reaching any of its life limits, exactly or past it (14 CFR 43.10(c): a
life-limited part that has reached its life limit is replaced, not fitted again; one that
reaches its limit while fitted stays on record, and the board grounds the aircraft at that
same point; a calendar life is reached when its last day ends), a
back-dated installation that overlaps a window, a correction that would orphan later
entries, a date more than five minutes in the future, negative hours or cycles. Every entry is judged at its own
date, against the records as they stood when it happened, so a state change back-dated
inside a work order's open window is accepted although the order was closed later; and a
write that would make an already recorded later entry impossible, such as a close dated
before a later state change or a retraction that would leave a part on two aircraft at
once, is refused and names that entry. A correction keeps the date of the entry it
corrects; to move a date, the entry is retracted and a new one written. Every entry
carries the sentence that it records what the entering person stated and that the
workbench does not certify airworthiness or return to service. `POST /entries` and `uasw record` reach the same
append, and two appends made at once, from one service or from a `uasw record` run beside it,
are checked one after the other: two corrections of one entry give one entry and one 409, and
an append or an ingest that waits more than 5 s for another write is refused with 503,
nothing written (an ingest's new aircraft and its flight are one write);
`GET /entries` and `GET /components` read the history with superseded entries
flagged. Writes need the token in `UASW_WRITE_TOKEN`, or come from this machine when none is
set, and a write without it is refused before its body is read; what that does not
protect against is in [LIMITS.md](LIMITS.md).

**A write journal that makes a change to the file visible.** Append-only in the application
is not append-only in the file: anyone with the SQLite file can edit it. So every entry and
every flight record is linked, in the transaction that writes it, into one hash chain
([`service/journal.py`](src/uas_workbench/service/journal.py), hashlib only): a sequence
number, a SHA-256 over the row's content in one canonical form, the hash of the link before it
and the hash of the link itself. The head is the last link's hash. `uasw verify` and
`GET /journal/verify` walk the chain from its first link, recomputing every hash from the rows
as they are now, and report the head, the length and, on a break, the first bad link and what
kind of break it is: an entry edited, a flight deleted, a link inserted mid-chain, a row
inserted with no link, each done directly in SQLite, is named by sequence number. What the
chain cannot tell on its own, because it has no secret and nothing outside the file: a tail cut
off with its rows, and a chain recomputed from the start by someone with the file and this
code, both verify alone; `uasw verify --head H`, given a head written down earlier, reports
whether the records are unchanged since it, and fails on both. That is all the line says:
unchanged since a head, never that the records are correct. A store written by 0.5.x is
journaled when first opened, after a note that what came before it is not tamper-evident.
Whatever a journaled row holds, verifying reports a break and never crashes, and `uasw verify`
given a path with no store at it says so (`refused (404)`) rather than making an empty store
there and calling it intact; so do the other commands that only read, and `uasw ingest` or
`uasw record` refused on such a path makes no store there either: a store is made by a write
that is accepted, or by `uasw seed` and `uasw serve`. The walk reads only the journaled rows,
so `uasw verify` first checks the file whole with SQLite's `PRAGMA integrity_check`, and so
does every other command when it opens an existing store, those that write (`seed`, `serve`,
`ingest`, `record`, `record retract`) and those that only read (`records`, `evidence`,
`fleet-records`, `export-static`, `record history`): a file damaged at rest, in pages the walk,
the write or the read would not have read, is one `refused (503)` line naming SQLite's
finding, exit 1, with nothing written and no `intact` or `unchanged` line (#104, #105, #107).
A command that only reads folds nothing into the file: a `-wal` a hard stop left is still
there, byte for byte, after it has answered or refused (#107). A `-wal` and `-shm` a hard stop left beside such a
file are kept, and belong to it: the line says whether the damage is in the file or only in
the `-wal`, or, beside a fold of the `-wal` into the file that a hard stop cut short or a reader
held back, that it cannot tell which (#111), and to move the file, its `-wal` and its `-shm` aside together before
putting a good copy where the file was, since a good copy put beside that `-wal` takes its
pages in (#108). A store whose `-shm` cannot be written (a read-only store's side files marked
read-only too) is one `refused (503)` line naming the side files, with nothing opened, since no
read can hold one state of the store without writing there (#112). A running service whose store
file is overwritten, truncated, replaced or removed from outside, or has one index key edited
in place, refuses every read and write of the store with 503 naming the cause, `/health`
included, rather than serving what it held in memory: it checks a file changed under it with
`PRAGMA integrity_check`, and `GET /journal/verify` runs that check before its walk (#107); restored in place, it serves the restored file, provided the service had no writes
of its own waiting in its `-wal` file (restore with the service stopped; see
[LIMITS.md](LIMITS.md)). `GET /journal/verify`
needs the write token, or comes from this machine when none is set, as the writes do: it
writes nothing, but it reads every row on a call, and [LIMITS.md](LIMITS.md) gives the measured
cost. The walk is a snapshot on the request thread's own reading connection, so a write made
meanwhile lands at once:
the store file is in SQLite's WAL mode, which means that while a service or a `uasw` command
has it open, `fleet.sqlite` has `fleet.sqlite-wal` and `fleet.sqlite-shm` beside it and the
file alone is not a complete copy. Copy or back it up with no store open (every command closes
the store when it ends, and the two side files go with the last connection; after a process
was terminated they stay until the next store opens and closes the file, so run `uasw verify`
first), or while the service runs with `sqlite3 fleet.sqlite ".backup copy.sqlite"`, which
reads a snapshot. In the
container the three files live together in the `fleet-data` volume.

**The assistant reads the ledger, and only reads it.** A sixth tool, over `GET /entries`,
gives it who recorded what, when, and what superseded what; superseded entries always come
back flagged, and the model is told never to present one as current. The grounding check
grew with it: an entry id, a person's name, a work order state or a work order id that none
of the fetched entries contain marks the answer unverified, an entry named without its
successor marks it unverified, and an answer that drew on entries must end with the sentence
that entries record what the entering person stated and that the workbench does not certify
airworthiness or return to service. Four more recorded runs show it: who recorded a work
order, what superseded what and why, which components were fitted to an aircraft, and a
question the ledger cannot answer, a certificate number, answered as not recorded; the first
three of these were recorded again in 0.3.0 for the same reason, and the second of them
again in 0.5.0. The seeded
fleet carries one synthetic correction so the page shows what superseding looks like.

**A draft evidence pack for OSO #03.** `uasw evidence SYN-04 --out pack.html`, or
`GET /aircraft/{key}/evidence.html` and the same as JSON at `/aircraft/{key}/evidence`,
gathers what the records hold for one aircraft under OSO #03 of the JARUS SORA methodology,
"UAS maintained by competent and/or proven entity": the sources relied on, with edition and
page (JARUS SORA Annex E editions 2.5 and 1.0, and the EASA means of compliance to OSO #3 of
December 2024, paraphrased and cited, never quoted); every OSO #03 item marked supported,
partly or not evidenced by this workbench, with the sections that support it; time in
service and usage from the logs; the programme status; each life-limited part's history;
and the maintenance log with superseded entries as history next to the entry that superseded
them. Its first lines say what it is: a draft for the operator's own review, which shows no
compliance, claims no robustness level, does not certify airworthiness or return to service,
and leaves the decision to the authority. It carries the generation time, the commit, the
workbench version and a hash over every entry read, so two packs can be compared. One
self-contained HTML document, printable to PDF from any browser, with the data label on every
printed page; no dependency was added. The live demo carries one sample for the synthetic
aircraft SYN-04. Of OSO #03, the workbench can evidence the maintenance log, the usage behind
the schedule and the life-limited part history, and nothing about instructions, staff
authorisation, competence, training, release to service, a procedure manual or third-party
validation; the pack says so item by item.

**A draft records summary against UAS.SPEC.050(1)(g).** `uasw records SYN-04 --out records.html`,
or `GET /aircraft/{key}/records.html` and the same as JSON at `/aircraft/{key}/records`, sets
what the workbench holds for one aircraft against the three record-keeping points of
UAS.SPEC.050(1)(g) of Regulation (EU) 2019/947 (as amended by (EU) 2020/639; paraphrased and
cited, never quoted): training records, maintenance activities, and operations with any unusual
occurrences, each marked supported by these records, partly, not evidenced by this workbench or
not known; the span of the records held against the point's 3-year minimum, as covered, not yet
3 years or not known; and the write journal's head with its verify result, "unchanged since
head H" when the chain verifies and the first bad link otherwise. Its title says what it is, a
draft for the operator's own review: it states no record as true, decides nothing about any
obligation, certifies nothing, and leaves the decision to the authority. It needs no token, and
so it never walks the journal on a call: it shows the walk the store already holds, made at
startup and again only after another connection changed the file, and moved on by the store's
own writes; `GET /journal/verify` stays guarded and walks on demand. One self-contained HTML
document, as the evidence pack is, and one sample for SYN-04 on the live demo. A record the
store cannot read leaves what depended on it not known, with the record named. Every read it
makes, the projection and the journal result included, is one snapshot of the store, which
holds no write lock: a write landing while it is built is in the next summary, never in half
of this one.

**The same summary for the whole fleet.** `uasw fleet-records --out fleet-records.html`, or
`GET /fleet/records.html` and the same as JSON at `/fleet/records`, carries the same title and
one row per aircraft with the three statuses and the span word that aircraft's own summary
gives (each row is that summary, reduced; there is no second implementation), the counts of
aircraft by point and status and by span, one journal head and verify result for the whole
store, and whether every stored record reads. Every row and the head are read from one
snapshot of the store, with no write lock held: a write landing while the summary is built
lands at once and is in the next one. It needs no token and reuses the walk the store already
holds, so nine aircraft cost no walk, and an outside commit costs the next build one walk, not
one per aircraft. One entry the store cannot read leaves the maintenance point and the span
not known on every row, as the board's liveness rule has it; a flight record that cannot be
read leaves only its aircraft's operations and span not known; the counts count each such row
as not known and nowhere else, and the summary is still written, with the record named. The
live demo carries it as `records/fleet.html` beside the SYN-04 sample.

## What remains

- No release to service is recorded, by design: the tool never releases anything, and a
  person's release is their own act. An operator who needs one on file records it today in
  the statement that closes a work order.
- Records are entered by a person and taken at their word: no users, no roles, no
  signatures, no certificate check, no attachment. The write token is a shared secret.
- A life limit is never reset by work. A life-limited part at its limit is replaced by
  registering and installing a different component, and the ledger refuses to fit one that
  has reached a limit; there is no time-since-overhaul.
- The assistant is a local command, not a live page feature; the page replays recordings.
  The grounding check catches invented numbers, ids and dates, not a wrong sentence built
  from real ones; the records under each answer are there so a reader can check the rest.
- A flight logged while the records show the aircraft grounded is reported, never prevented,
  and never explained: the tool holds no permission for a check flight and no release, cannot
  tell an entry dated back from one made on the day, and takes the operator's word for which
  airframe a log belongs to. The assistant has no tool for these findings yet, so it cannot
  be asked about them; the nine recorded runs were made with six tools and stay as recorded.
- A cycle is one flight record. Logs give no battery pack identity or cycle count
  ([LIMITS.md](LIMITS.md)), so pack assignment is an operator record, and a flight that opens
  and closes without landing in between still counts as one cycle.
- The limits in `fleet.toml` are placeholder defaults with their sources, not the values of
  any product; a user of the tool replaces them with the manufacturer's.
- In maintenance and AOG come from open work orders only; no rule turns a due item into a
  work order on its own.

## What comes next

The plan the study set for v0.1 is complete. What follows is decided by use: the first
real fleet's logs and records will show which of the limits above bite first.

## Scope and non-goals

- Civil fleet sustainment only: usage counters, component life, inspections, readiness.
- No payload, targeting, engagement or counter-UAS functions.
- Public and synthetic data only. Not endorsed by, or built for, any armed force.
- No raw flight logs in this repository. The only log files are four short excerpts with
  positions and device ids removed. See [DATA.md](DATA.md).

## Run it

```bash
docker compose up                 # then open http://localhost:8000/docs
```

The port is bound to this machine only. Writes from the host reach the container from the
Docker bridge, not from loopback, so they need a token:
`export UASW_WRITE_TOKEN=$(openssl rand -hex 16)` before `docker compose up`, then send it as
`Authorization: Bearer <token>`. Without one, reads work and every write is refused with a
message saying so.

Without Docker:

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev,fetch]"
uasw serve                                        # seeds an empty store; /docs, /metrics
uasw ingest my-aircraft path/to/log.ulg           # add your own logs (never stored raw)
uasw export-static --out site                     # the static demo, from the same store
ruff check . && mypy && pytest                    # no network, no downloads needed
```

CI runs lint, strict types, the tests and the static export on every pull request on Linux
(Python 3.12) and on Windows (Python 3.12 and 3.14); the container and the headless-browser
checks run on Linux.

Recording maintenance, on the same store, with no service running:

```bash
uasw record work-order open my-aircraft --state awaiting_parts --by "A. Name, maintenance" \
    --statement "replacement propeller set on order after a ground strike"
uasw record component register PACK-7 --kind "battery pack" --since 2026-08-01 --by "..." --statement "..."
uasw record component install PACK-7 my-aircraft --by "..." --statement "..."
uasw record inspection my-aircraft "100-hour inspection" --by "..." --statement "..."
uasw record history my-aircraft --all              # superseded entries too, with the reasons
uasw record time-in-service my-aircraft 42.5 --supersedes 9 --by "..." --reason "misread" \
    --statement "..."                             # a correction; it keeps entry 9's date
uasw record retract 12 --by "..." --reason "entered against the wrong aircraft"
uasw records my-aircraft --out records.html       # the draft records summary, journal head in it
uasw fleet-records --out fleet-records.html        # the same for the whole fleet, one row each
uasw verify                                        # the journal: intact or the first bad link
uasw verify --head <head from an earlier run>      # unchanged since that head, or not
```

The assistant needs a running service and a local [Ollama](https://ollama.com) with the
model pulled (`ollama pull qwen3:8b`, Apache-2.0, about 5 GB; no account, no key). Ollama
listens on localhost only. Then:

```bash
uasw ask "which aircraft are not serviceable, and what stops each one?"
uasw ask "..." --as-of 2026-10-01T00:00:00Z --record run.json   # a replayable recording
```

The census that measured the public logs is `uasw-census` (`px4-select`, `px4-fetch`,
`alfa-fetch`, `run`); downloaded logs go to `data/raw/`, which git ignores.

## Licence

Code: Apache-2.0. Data and fixtures keep their own licences and attributions; see
[DATA.md](DATA.md).
