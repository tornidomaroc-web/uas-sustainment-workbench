# Changelog

## Unreleased

- `uasw ask` ends with one `refused` line on stderr and exit 1, never a traceback, when the
  service does not answer or the model backend fails (issue #49). A service that is not
  running, a host that does not resolve, a connection that times out, is reset or carries no
  HTTP reply is `refused (unreachable): the service at <url> did not answer: <reason>`, with
  no status since none was received (a 503 is the service's own answer for a record it cannot
  read); a backend
  that answers an error status is `refused (502): the model backend at <host> answered <status>
  <reason>`, with the backend's own `error` sentence when its reply carries one, a backend that
  does not answer is the same line with `did not answer: <reason>`, and a model that is not
  pulled is `refused (502): model '<tag>' is not pulled; the backend has [...]` (it was a bare
  `LookupError`). A URL or host is shown without its userinfo, query or fragment, and a
  reason that repeats them is scrubbed, so no credential reaches the line. The library raises `ServiceUnreachable` and `BackendError`, both
  `RefusedCall`, now in `assistant/errors.py` and re-exported where they were; a refusal a
  tool call raises keeps its type with the tool and path filled in. What is caught is exactly
  `urllib.error.URLError`, `TimeoutError`, `ConnectionError` and `http.client.HTTPException`
  from the exchange itself, nothing wider: the model is never given a failed or partial reply,
  nothing is printed on stdout, `--record` writes nothing, and the existing 503 and 404 lines,
  a successful `uasw ask`, every response, record, pack and hash are unchanged.
- The repository hygiene guard's reserved-word and product-name checks screen a plural as
  its singular: each split word, and the second word of each adjacent pair, is compared
  with a trailing s removed as well as as written, so a plural identifier or a plural in
  prose is reported like the word itself; a run of capitals ending in a plural s is kept as
  one word, so the bullet's two-word term is caught in the plural as it is written. The two
  checks are functions over a directory and a list of names, and the suite plants each
  reserved word and term, read at run time from the README's scope bullet so no test spells
  one and failing unless they account for every reserved digest, in the singular and the
  plural, in snake_case, camelCase, alone and hyphenated, and the bullet under its own file
  name and under another, so the allow-list is shown keyed by path and exact text. Nothing
  in the tree is newly reported; no response, record, pack or hash changes (issue #53).
- The repository hygiene guard screens every tracked file, binary fixtures as their printable
  strings and source identifiers split into words, for the words the README's scope bullet
  reserves, held as hashes like the product names; only that bullet and the upstream field
  and parameter names inside the pinned PX4 excerpt are allowed, each by its own text, never
  by file. The one local name that used a reserved word, in the JSON log formatter and the
  two synthetic-log test builders, is renamed; no response, record, pack or hash changes
  (issue #51).
- The JSON log handler writes to whatever `sys.stderr` is when a record is emitted, not to
  the stream it was when `configure_logging` was first called; `uasw` closes the store a
  command opened when the command ends, and the assistant closes the `HTTPError` it converts
  into a refusal (issue #48). The test suite treats a resource left open as an error, as
  Python 3.13+ warns for an unclosed SQLite connection, and its helpers close what they
  open; since `tests/conftest.py` closes every test's stores, `test_cli_closes_its_store.py`
  checks on any Python that each store a command opened is closed when `main()` returns,
  before that teardown; the suite is green in any module order and on Python 3.13 and 3.14.
- Every ledger entry and flight record is linked into a write journal in the transaction that
  writes it (`service/journal.py`, hashlib only): a sequence number, a SHA-256 over the row's
  canonical content, the previous link's hash and the link's own hash, one chain for both
  tables. `uasw verify [--head H]` and `GET /journal/verify?head=H` (guarded as the writes
  are, token or loopback, since it holds the store's write lock while it reads every row:
  3.9 s for 100,000 links, in `LIMITS.md`) walk it from the first link and report the head,
  the length and, on a break, the first bad sequence number and its kind (`sequence`,
  `duplicate`, `previous_hash`, `missing_row`, `content`, `link_hash`, `unjournaled`); so an
  entry edited, a flight deleted, a link inserted mid-chain or a row inserted with no link,
  each done directly in the SQLite file, is reported, and so is a note put on an entry or
  flight link, which no hash covers. Whatever the file holds, verifying reports and never
  raises: a record that cannot be parsed or hashed and a note that is not the migration note
  are each a `content` break at the link, never a traceback or a 500. A truncated tail
  and a chain recomputed from the file verify alone and fail only against an earlier head given
  back, and an append made in the file with its link computed as the store would is not told
  from one made through the store; `LIMITS.md` says so. A store written by 0.5.x is journaled
  when first opened, after a link holding the note that what came before it is not
  tamper-evident. A fresh seeded store verifies with one pinned head. Every existing response,
  every stored record and the evidence pack's hash are byte for byte what they were; the result
  line says unchanged since a head, never that the records are correct.
- A stored record the store cannot read (issue #43: an entry nested 100,000 deep, a record
  that is not JSON, a field missing or of the wrong type, a time with no UTC offset, each
  edited directly in the SQLite file) no longer stops the service at startup or turns every
  ledger read into a 500, and is never skipped either. Every record is read through one path
  in `service/store.py`, which raises `UnreadableRecord` naming the table, the row, the
  aircraft or component and why; the projection raises the same for an entry whose details
  cannot be used. The service starts and logs the record once as a warning; the aircraft it
  leaves unknown count as `unknown` and a gauge it leaves uncomputable is NaN, never 0;
  `GET /health` answers `degraded` with the record named under `unreadable`, from what the
  store read at startup or after another connection last changed the file, never by reading
  every record on a call (4.1 ms with 128,718 entries, in `LIMITS.md`);
  every read that depends on the record answers 503 with the same sentence, as does a write
  judged against the projection, with nothing written; `uasw` prints `refused (503): ...` and
  exits 1; and the evidence pack is still written, since part 2 of 0.6.0 puts the verify
  result in it, with every value that depended on the record as `{"unknown": ...}`, every
  item it would have supported `not evidenced` for that reason, and the label saying so. The
  whole-ledger projection cache is keyed on SQLite's `data_version`, so an edit committed by
  another connection while the service runs is met on the next read, not hidden by a copy
  made before it. `uasw verify` keeps reporting the row as a `content` break at its link.
  Every response on a readable store, the OpenAPI document, the seeded journal head and the
  pack hash are byte for byte what they were.
- `uasw ask` no longer ends in a traceback when the service refuses a tool call (issue #46).
  A read the model asked for that answers an error status, a 503 naming a stored record the
  store cannot read, or a 404 for an aircraft key the model made up, ends the run: the model
  is not asked again, so it never holds the refusal as a tool result it could phrase as a
  value or ground an answer on, and no answer, grounding check or recording carries the
  call. `uasw ask` prints `refused (503): <the service's own sentence>` (or the status it
  answered) and exits 1, as every other command does; 401, 403, 422 and a reply with no usable
  body take the same line, with the status line's reason when the body holds no `detail`.
  `ask()` raises `RefusedCall` with the status, the sentence, the tool and the path, and
  `service_caller()` is the caller `uasw ask` builds. An unknown tool or a bad argument is
  still shown to the model, since that is its own mistake to correct. No response, record,
  recording or hash changes.

## 0.5.2

What changed in behaviour is the cost of one refusal. A `.bin` that holds no DataFlash format
definition is refused before pymavlink opens it, so a file that is not a log writes nothing to
standard error whatever its size, where 0.5.1 let pymavlink print one line per byte. The
refusal, what is stored (nothing) and the record of every readable log are unchanged. The rest
is a test that keeps this file's quoted examples checked after a release.

- `POST /ingest` and `uasw ingest` refuse a `.bin` that holds no plausible DataFlash format
  definition before pymavlink opens it, so a file that is not a log writes nothing to stderr
  whatever its size, where 0.5.1 let pymavlink's indexer print one `bad header` line per byte
  (13 for 16 bytes, 16.7 million for 16 MiB); the 422, its wording, what is stored (nothing)
  and the record of every readable log are unchanged, and `LIMITS.md` has the measurements and
  what still prints.
- `tests/test_cue_examples.py` checks the cues quoted in the newest released section of this
  file as well as in Unreleased, so a release no longer takes its examples out of the check, as
  0.5.1 took its three. The newest released section's cue count is pinned, so the next release
  fails the test until its count is written down, 0 included; an older section stays history. A
  checked section that must quote an old cue whole names it in the test's `HISTORICAL`, which
  holds it to being quoted there and stale.

## 0.5.1

What changed in behaviour is ingest. A `.bin` that is not a DataFlash log, and a `.bin` or
`.ulg` that holds definitions but no recorded data, were stored as a 0.0 s flight; they are now
refused with 422 and nothing is stored. A ULog that fails to parse and an empty `.bin` were
already refused, but left their file open; they no longer do. `uasw ingest` now refuses as the
API does and reads past a refused file, where it stopped with a traceback at the first one (or,
for an unsupported file type, exited 0). The records of every readable log are unchanged. The
rest is repository tooling, test dependencies and corrected documentation examples.

- `.claude/settings.json` and `.claude/hooks/cloud-guard.js`, for Claude Code cloud sessions on
  this repository. Before every shell command and file edit, a hook loads the guard only when
  `CLAUDE_CODE_REMOTE` is `true`, which Claude Code sets in cloud sessions and never locally.
  In a cloud session the guard refuses merges, releases, tag creation and tag pushes, pushes to
  main, forced and deleting pushes, writes through `gh api`, docker, ollama, and edits under
  `.claude/`; it allows the rest, including pushing the session's own branch. In a local
  session it refuses nothing, and no permission rule is set, because one would bind local
  sessions too. It reads command text, so it is a convenience in front of the server-side
  rules (the branch rule on main and the `v*` tag rulesets), not a wall.
  `tests/test_cloud_guard.py` pins what it refuses and allows, both ways.
- `.github/CODEOWNERS` names the repository owner for every path. With the main ruleset that
  needs one code owner's approving review, an approval from an app never counts toward a merge;
  the owner merges through the ruleset's admin bypass (`gh pr merge --admin`), which exempts
  the review only, never the three checks.
- The `life/cue.py` docstring's example of a grounded-flight cue now uses the hand-built test
  aircraft (`flown unserviceable 2026-03-11 10:00`), since the 0.5.0 demo fleet holds no such
  flight; it used a flight of the 0.4.0 seed.
- Two more cue examples now show what the 0.5.0 demo gives: the README's past-limit example is
  `BAT-04B past calendar limit 2026-08-31`, where it showed BAT-04A one cycle past, which 0.5.0
  never reaches; the `life/cue.py` grammar table shows `not known: time in service` where it
  showed a deferred defect, which no record holds.
- The `dev` extra installs `httpx2` instead of `httpx`, and asks for Starlette 1.3 or later, the
  first release whose test client runs on `httpx2`. With plain `httpx` that test client raised a
  deprecation warning on every test run. `tests/test_test_client.py` fails if it comes back.
- `read_ulog` opens the log itself and hands pyulog the handle, so a log that fails to parse no
  longer leaves its file open on the ingest path; `tests/test_flight_px4.py` fails if it does.
- A `.bin` upload with no DataFlash message at all is refused with 422 and nothing is stored,
  as a junk `.ulg` already was; an empty `.bin` no longer leaves its file open.
- A `.bin` or `.ulg` with definitions, metadata or parameters but no recorded data message is
  refused with 422 and nothing is stored, where it was a 0.0 s flight; one data message of any
  type, read by the tool or not, still makes a flight, and every fixture's record is unchanged.
- `uasw ingest` no longer stops with a traceback at a log it cannot read: it prints one
  `refused (422)` line naming the file, with the reason `POST /ingest` gives, stores nothing for
  it, reads the files after it, and exits 1 once all are read if any was refused.
- `uasw ingest` refuses a file whose type it has no reader for in the same way, with the
  `unsupported file type` text `POST /ingest` gives, and counts it toward exit status 1; it
  logged a line and exited 0.

## 0.5.0

One rule, stated once and applied everywhere: **a limit counted in completed units is reached
when its last unit completes.** A cycle completes when its flight is over, an hour when it
has been flown, a calendar day at midnight. Three changes to the rules follow from it; then
the tool reports a flight logged while the records show the aircraft grounded; then the
synthetic fleet is generated so that its aircraft do not make such flights. The order
matters and is kept below: the first two were measured on the 0.4.0 seed, unchanged, and
the third changes that seed. The version string became 0.5.0 with the release.

**What the released 0.4.0 seed recorded, stated plainly.** One take-off on a battery pack
already at its life limit, which 0.4.0's own board called serviceable (SYN-01, 2026-08-06
11:07:54, BAT-04A at 300 of 300 cycles), and six flights by aircraft that 0.4.0's own board
showed as unserviceable or in maintenance: SYN-01 at 12:09:19 that day and on 2026-08-08 at
12:24:12, one and two cycles past the limit; SYN-05 on 2026-08-07 at 08:41:42, on 2026-08-08
at 08:54:49 and on 2026-08-10 at 09:18:30 and 10:02:32, with a work order open that said to
inspect before the next flight. The pack was then recorded as "placed in storage" at 303
cycles. None of it was a real aircraft or a real flight; all of it was the demo teaching
what no operator should do, and none of it was flagged. The v0.4.0 tag and its release are
not moved or edited.

### Changed: a life limit reached grounds the aircraft

Until now a fitted part with nothing left of its cycles or hours read "due soon" and the
board read serviceable: "has 0 cycles left of its 300-cycle life limit". The ledger has
refused to fit a part in exactly that condition since 0.3.0 (14 CFR 43.10(c), "after it has
reached its life limit"), so the two rules disagreed at the limit: a part the ledger would
not put on an aircraft could take off on one. Now a part whose cycles or hours have reached
its limit, exactly or past it, is `overdue` and the aircraft is unserviceable. Before an
aircraft's first log, a limit reached grounds it as a limit past already did.

New sentences, one per basis, for remaining exactly 0:

- "battery pack BAT-04A on aircraft SYN-01 has reached its 300-cycle life limit"
- "propeller set PROP-01 on aircraft SYN-01 has reached its 300 h life limit"

Sentences that can no longer be produced: "... has 0 cycles left of its 300-cycle life
limit" and "... has 0.0 h left of its 300 h life limit". Every other sentence of the engine
is word for word what it was, including the calendar ones.

The calendar rule was checked against the same reasoning and stays: a calendar life of N
calendar months runs through the last day of its month (14 CFR 91.409(a) counts calendar
months to their last day), so on that day part of the last unit is left and the part is
within its life; it is past from the next day. "0 days left, due by <date>" is therefore
still a valid part, where "0 cycles left" was not.

Not changed: an inspection at exactly zero hours remaining still reads "due in 0.0 h". The
100-hour inspection has a tolerance for the flight that overruns it (91.409(b)); a life
limit has none. LIMITS.md says so.

### Changed: a flight's cycle and hours count once the flight is over

Until now a log counted from its UTC start, so a cycle was counted as the aircraft took
off, and with the change above the board would have grounded an aircraft at the start of
its last permitted flight. A record holds no landing time (flight time is a total airborne
within the log, and the UTC start is the log's start), so a log now counts from its end,
start plus span: the last instant its flight can have ended. `life.engine.log_end` and
`life.engine.counted` are that rule, and everything that asks "by this date" uses it: the due
list, time in service, a component's usage when the ledger judges an install or derives an
inspection's hours, and the logs a dated board row, reconciliation or evidence pack lists
(`flights_until`). At a date inside a log, that log is not listed and not counted; before
0.5.0 it was both.

A flight with no recorded end: a log with a UTC start always has an end, even when its
flight time is not known (one cycle, no hours, from that end); a log with no UTC start counts
for the airframe at any date and for no component, as before; flight no log covers counts
from the end of the log it followed, the earliest it can have been flown, so a limit is never
reported later than it was reached (LIMITS.md).

### Changed: a part may be fitted on the last day of its calendar life

0.3.0 refused an install on the last day of a part's calendar life with "its
24-calendar-month life limit ends that same day, <date>", while the board called a fitted
part valid on that same day. By the rule above the life is reached when that day ends, so
the refusal is withdrawn and that sentence no longer exists. From the next day the install
is refused as before, with the unchanged sentence "its 24-calendar-month life limit ended on
<date>". This is the only 409 whose rule or words changed; no 422 changed. The board and
the fit refusal now agree at the limit on hours, cycles and calendar, and
`tests/test_reached_limit_and_landed_usage.py` holds that through the ledger on each basis.

### Added: the cue of a reached limit

"BAT-04A reached 300-cycle limit", "PROP-01 reached 300 h limit"; with an id too long for
that form, "<id> reached 300 cycles" and "<id> reached 300 h". The checker refuses "reached"
on a cue whose sentence does not say "has reached", and any other word on a cue whose
sentence does: a part one cycle past is not merely at its limit, and one at its limit is
neither "left" nor "past". The demo page's two captions say "reached or past" and "at or
past" where they said "past".

### Measured on the 0.4.0 seed, unchanged: 0.4.0 against the changes above

Same seed, same records, 1018 answers compared: the four dated routes with and without cues
for all nine aircraft at the 24 scene plan times and at now, plus flights and reconciliation.

- **58 answers differ, at 8 of the 24 plan times; none at 2026-10-01 and none at now.** Six
  of the eight are the first second of an aircraft's first log (SYN-01, 02, 03, 04, 05, 07)
  and one, 2026-08-06 11:08, is inside SYN-01's third log: that log is no longer listed or
  counted, so the row's `flights` and `flight_time_known_s` are lower and each hours and
  cycles item reads one flight less. The eighth is 11:07 that day, below.
- **One board state differs at a plan time:** SYN-01 at 2026-08-06 11:07 reads unserviceable
  ("has reached its 300-cycle life limit") where it read serviceable. At 11:08 it is
  unserviceable in both, with the reached sentence where it had "1 cycle past". SYN-03 at
  2026-08-11 09:00 keeps its state and reads 1.1 h overdue where it read 1.7 h.
- **Over the whole period**, SYN-01 turns unserviceable at 2026-08-06 09:07:54, when its
  second log ends with BAT-04A at 300 of 300, not at 11:07:54 when its third log starts; each
  later count moves from a log's start to its end. No other aircraft's state changes at any
  instant; SYN-03's overflown hours step at log ends.
- **The nine recorded assistant runs replay identically**, records and text, and stay
  grounded: none was recorded again and no model was run.
- **No ledger hash changes** in any of 225 evidence packs (nine aircraft, 25 dates); the
  published SYN-04 sample keeps
  `3515dcb1fc7f1282c73fad1c6fbc9ebe39df61537c5629145a4b55d96f42eabf`. Eight packs differ in
  their computed sections, all at the dates above.

### Added: a flight logged while the records show the aircraft grounded is reported

A log is evidence: it is never refused, and its flight counts toward usage whatever the
board said. Until now nothing compared the two. `life/grounded.py` compares every log's
start with the board state the records give at that instant, and a flight that began while
that state was a known unserviceable, in maintenance or AOG is reported:

- "a flight of aircraft FX-01 was logged from 2026-03-11 10:00:00 UTC while the records
  show the aircraft unserviceable at that time: battery pack BAT-FX on aircraft FX-01 has
  reached its 300-cycle life limit"

One sentence per flight: the aircraft, the log's start, the state, and every cause that
grounded it at that instant, worst first, in the board's own sentences. With `?cues=true`,
`cue` ("flown unserviceable 2026-03-11 10:00", "flown in maintenance 2026-03-10 10:00",
"flown AOG 2026-03-10 12:00") and `cause_cues`, the existing cues of those causes ("BAT-FX
reached 300-cycle limit"). These examples, and the sentence above, are what the hand-built
test aircraft FX-01, FX-02 and FX-03 give (`flown_grounded()` in
`tests/test_grounded_flights.py`); the demo fleet generated since 0.5.0 holds no such
flight. The sentence says the records and the log disagree; it does not say which is
right, and LIMITS.md lists what it cannot know: an entry dated back, a time typed to the
day, a log filed under the wrong airframe, a check flight the operator permitted.

What is judged. The instant is the log's start itself, on everything dated up to and
including it, the log's own flight excluded (a log counts from its end): the answer the
board gives at `as_of=<the log's start>`, so every finding can be checked there. A log
that starts the second before a work order opens is not reported; one that starts at the
same instant is.

What is never a finding. A state that is not known: a flight before any maintenance record
existed, and every log of an aircraft with no record. A log with no UTC start, which cannot
be placed in time. A log with 0 s airborne, or one that cannot say whether the aircraft
flew. The second upload of the same log. Each is listed as not judged, with why, and the
answer carries how many logs were judged, so an empty list is never mistaken for a clean
one. The two public showcase aircraft are never judged, by key, whatever the store holds.

Where it is served, and why there.

- `GET /aircraft/{key}/grounded-flights` and `GET /fleet/grounded-flights`, with `as_of` and
  `cues`: the logs that had ended by then, judged, with the findings in the order the logs
  started and the logs not judged.
- The evidence pack, in its usage section, as sentences: a pack that lists an aircraft's
  flights and its maintenance log and stays silent where the two disagree would be hiding
  what a reader of it looks for first. No pack's ledger hash changes.
- The demo page, above the fleet table, with the count of logs judged and not judged.
- Not the board, the due list, a board row's `findings` count or reconciliation. The board
  is the state now with its reasons, and a past flight is neither; and those are the answers
  the nine recorded assistant runs read, which stay byte for byte.
- Not the assistant: a seventh tool would change what the model is offered, and the nine
  runs, made with six, would no longer be runs of the assistant as it stands. That waits for
  a change in which runs are recorded again.

Measured on the 0.4.0 seed, still unchanged, the changes above against this one: all 1021 existing
answers compared (the four dated routes with and without cues for nine aircraft at the 24
scene plan times and at now, plus flights, reconciliation, the fleet's findings, the ledger
and health) are identical; the nine recorded runs replay identically and none was recorded
again; no ledger hash changes in 225 evidence packs and each pack is identical apart from
the new part of its usage section; the published SYN-04 sample keeps its hash and gains
"Of the 3 logs judged, not one started while the records show the aircraft unserviceable,
in maintenance or AOG." The static site's `fleet.json` gains one key, `grounded_flights`,
and every existing key is identical. The new fleet route answers 40 logs, 32 judged, 8 not
judged, 7 findings at now; at the plan times the findings appear as their logs end: none
through 2026-08-06 11:08, two by 2026-08-07 08:27, five by 2026-08-08 13:59, seven from
2026-08-10 13:59.

### Changed: how the synthetic fleet is generated

A generation change, made so that the demo fleet no longer records flights by grounded
aircraft. With the rules above on the 0.4.0 seed, seven logged flights started while the
records showed the aircraft unserviceable or in maintenance (the six and the take-off listed
at the top of this section), and the finding reported exactly those seven. Three things
change in `fleet/synthetic.py`:

- **BAT-04A starts three cycles short of its limit, not two** (297 cycles before, where it
  had 298). SYN-01's third flight, from 2026-08-06 11:07:54, is then the pack's 300th cycle:
  a permitted flight, and its last. The aircraft is serviceable when it starts and through
  it; when its log ends at 11:39:19 the pack has reached its limit and SYN-01 is
  unserviceable, until the pack comes off on 2026-08-13 at 08:00.
- **Logs end at grounding.** Every flight is drawn as before, so every random draw is what
  it was; then each log that would start while its aircraft's own records show it
  unserviceable, in maintenance or AOG is left out, judged by the comparison the service
  reports with (`life/grounded.py`), repeated until none remains. SYN-01 keeps three logs of
  five and SYN-05 two of six; the fleet has 30 synthetic logs where it had 36. The rule is
  the generator's, not a list of logs: a different seed is filtered the same way.
- **BAT-04A's removal reads "tagged unserviceable and segregated"**, which is what is done
  with a life-expired part, where it read "placed in storage". It comes off at exactly 300
  cycles, where it came off at 303.

What does not change, pinned by digest in `tests/test_synthetic.py`: the flights of the five
other aircraft, the logs SYN-01 and SYN-05 keep, and all 60 entries but BAT-04A's
registration (its cycles) and removal (its statement), byte for byte. The entry ids are the
same, and every entry is still accepted by the ledger's write rules in order.

Recorded again: runs 02 and 07, on 2026-10-01, with qwen3:8b at the same weights (digest
`500a1f067a9f…`, checked before recording) and the same computation date. Run 02 reads the
board rows, where SYN-01's `flights` is 3 for 5 and SYN-05's 2 for 6; run 07 reads SYN-01's
entries, where the removal's statement changed. Each was run once, passed the grounding
check, and is kept as it came; no attempt failed and nothing was chosen between answers. Run
07's answer is word for word the earlier one. Run 02 names the same three aircraft with the
same causes; its closing sentence says the other aircraft "are either serviceable or have
unknown maintenance status" where it said "or have no maintenance record entered", which is
less exact and stays. The seven other runs replay unchanged and were not touched.

Measured, the 0.4.0 seed under the rules above against the new seed:

- **Findings: none**, where there were seven. 34 logs (30 synthetic, 4 real), 26 judged, 8
  not judged, where there were 40, 32 and 8.
- **Ledger hashes:** the published SYN-04 sample keeps
  `3515dcb1fc7f1282c73fad1c6fbc9ebe39df61537c5629145a4b55d96f42eabf`. SYN-01's changes, from
  `730f95f06daf78db0a7be7806fb1270acfcff660b72989b839857c959e9249ec` to
  `eec1794ac2b29ec2937d72ab04bae3905936524f2455483c1c7631fa6b6f231c`, at every date from the
  removal on, because that entry's statement changed. No other aircraft's changes.
- **Board states at the 24 scene plan times and at now: two differ**, both SYN-01, at
  2026-08-06 11:07 and 11:08, serviceable where the unchanged seed read unserviceable. Over
  the whole period SYN-01 turns unserviceable at 11:39:19 that day (it turned at 09:07:54),
  with "has reached its 300-cycle life limit" until the pack comes off, never "past". Every
  state at 2026-10-01 and at now is what it was.
- **Other answers:** 242 of 1021 differ. From their grounding on, SYN-01's and SYN-05's rows
  count fewer flights (3 for 5, 5534.2 s for 7516.9 s; 2 for 6, 2194.6 s for 7139.8 s) and
  their hours and cycles items read that much less; SYN-04's lent pack BAT-01B reads 272
  cycles left where it read 270, having flown two flights fewer on SYN-01.
- **The demo page at now:** 42 values of `fleet.json` differ, all of the kinds above, and
  the section of flights logged while grounded reads none of 26 logs judged where it listed
  seven of 32. The SYN-04 sample pack differs in five values, all BAT-01B's two cycles.

Tests: 314. 20 in `tests/test_reached_limit_and_landed_usage.py`, 16 of which fail
on 0.4.0; 35 in `tests/test_grounded_flights.py` and two in the browser test for the
finding: every log of the seed accounted for, each state and each not-known case on a
hand-built fixture through the ledger, the showcase exclusion, the instant judged, an upload
for a grounded aircraft accepted and counted, five mutations of the rules that each change
the answer, and a checker that refuses each altered property of a finding and of its cue.
With the seed, the test that pinned its seven findings pins none; the routes, the pack and
the page are tested on three hand-built aircraft added to the demo fleet (five findings),
and the page is rendered once with none and once with five; a test puts the left-out logs
back and finds six reported; `tests/test_life_fleet.py` holds SYN-01 serviceable through
its third flight and unserviceable from the end of that log, and SYN-05 with no log after
its work order opens. Tests that counted SYN-01's five flights count three (302 cycles for
304 in three refusal sentences, whose words are otherwise the same).
Seven existing tests in five files changed because their rule changed, each saying
so where it changed: the cue mutation test accepts a reached cue as its overdue example; the
0.3.0 refusal sentences are five, not six; the SYN-01 story test follows the unchanged seed
under the new rule; two install tests expect a fitted part at its limit to ground the
aircraft, and a third expects an install on the last calendar day to be accepted; time in
service at a first log's start is the total entered, the log's own flight counting from its
end.

Changed: `life/engine.py`, `life/cue.py`, `life/grounded.py` (new), `ledger/validate.py` (the
calendar boundary of the fit refusal), `service/app.py` (`flights_until`, the two new routes),
the usage section of the evidence pack, the static export (one new key) and the demo page
(two captions, one new section), `fleet/synthetic.py` (the generation), recorded runs 02 and
07. Not changed: the ledger's file format, the shape of every response that existed, the
seven other recorded runs, the evidence sample's hash. The release commit changes the
version string to 0.5.0 and the examples of a finding in this file and README.md, and no
code.

## 0.4.0

### Added: a short cue beside every sentence, on request

Every reason and due item is one full sentence, up to about 230 characters, because the
sentence is the record's word; a wall board cannot carry that. With `?cues=true` on
`/aircraft`, `/aircraft/{key}`, `/aircraft/{key}/due` and `/fleet/due`, each due item gains
`cue` and the board state gains `status_cue` and `status_cues`, one per reason in the same
order: at most 40 characters, built from the same fields as the sentence by one grammar in
`life/cue.py`, never typed per case. The sentence stays exactly as it is.

What a cue keeps and drops. Kept: the part id or inspection name, the number, its unit and
the direction. Dropped: the aircraft key (a board row names it), the regulation, the
tolerance explanation, a calendar item's count of days (its date stays), and a work order's
description (a person's words, never cut). A cue says what its state says and nothing
stronger or weaker: "PROP-01 102.9 h left of 300 h", "BAT-01A 206 cycles left of 300",
"BAT-04B 17 days left, due 2026-08-31", "100 h insp due in 82.5 h", "annual insp due by
2027-04-30"; past a limit, "BAT-04A 1 cycle past 300-cycle limit", "BAT-04B past calendar
limit 2026-08-31", "100 h insp 12.0 h overdue"; inside a tolerance, "100 h insp 3.6 h
overdue, in tolerance"; not known, "100 h insp: time in service not known"; open work orders,
"AOG awaiting parts since 2026-08-14", "in maintenance since 2026-08-07", "deferred defect
since 2026-08-07"; a board state's own cue is the state, or "not known: no maintenance
record" and "not known: time in service".

Proven by `tests/test_cues.py`: every cue the seeded fleet produces at every scene plan time
and at now is within the limit; every cue names the same part, numbers, unit and state as its
sentence, judged by a checker that a mutation of each property fails; every cue passes the
assistant's own grounding check against its item, so it adds no number, id, date or name the
records lack.

Why on request. Adding a field to the responses would change what four of the nine recorded
assistant runs replay and what the evidence pack holds. The plain response models now forbid
extra fields, so a response is one shape or the other; without the option the bytes are
exactly what they were, the recordings replay unchanged, the evidence pack does not carry
cues, and the assistant does not read them.

Changed: `life/cue.py` (new); the `cues` option on the four routes and the cued response
models (`cue`, `status_cue`, `status_cues`); the plain response models forbid extra fields;
the version string. Not changed: the ledger and its file format, every response without the
option, the seed, the nine recorded runs, the published evidence sample's hash.

### Documentation: corrections to the 0.3.0 text, made after the v0.3.0 tag

Three sentences about 0.3.0 were wrong in wording, not in behaviour, and are corrected in
this file and in README.md. The files inside the v0.3.0 tag and the v0.3.0 release notes keep
the earlier wording; the corrected text below is what the documentation has said since
2026-09-29, and no code, test, recording or version changed with the correction. It is
listed under 0.4.0 because 0.4.0 is the first release whose files carry it: the v0.3.0 tag
is not moved, and no 0.3.1 was cut for wording alone.

- The 0.3.0 heading "Refused: fitting a part that is past a life limit" now reads "Refused:
  fitting a part that has reached a life limit". The rule under it always refused a part
  exactly at its limit too.
- The 0.3.0 section said every re-recorded answer "passed the grounding check on its first
  recording". It now also says that the first attempt at four of the five runs failed with
  a server error while the model was loading and produced no answer, so each was run once
  more, and nothing was chosen between answers.
- README.md said the first five recorded runs were "recorded once with `--record`". Two of
  them, and three of the four that follow, were recorded again in 0.3.0; README now says so.

Found in the same sweep and corrected: README.md described the ledger refusing a part
"after reaching or exceeding" a limit, next to a citation of 14 CFR 43.10(c), whose words
are "reached its life limit"; its list of what remains said the ledger refuses to fit a part
"past a limit", where it refuses one that has reached a limit; and its list of board states
left out "not known", which 0.3.0 added.

## 0.3.0

### Corrected: time in service before an aircraft's first log is not known

The hours before the first log are entered as one total: the time in service the aircraft
had reached by the first log this tool holds. Until now the engine used that total at any
earlier date too, so a query about a date before the first log printed a time in service
no record supports. Reproduced before the change on the seeded fleet: SYN-03, whose 110.0 h
before its first log of 2026-08-11 were entered on 2026-06-04, read 110.0 h on 2026-07-01,
and its 100-hour inspection read "10.0 h overdue, within the 10 h tolerance", a state that
reads as leave to fly, on a date the tool knows nothing about. Every synthetic aircraft read
"serviceable" at dates before its first log for the same reason.

The rule now: before an aircraft's first dated log, when hours before it were entered, the
time in service is not known. The inspection items that depend on it are in a state of their
own, `unknown`, with `used` and `remaining` not known and a sentence that says why. The board
is not known with that reason, unless an open work order or a life limit already past
grounds the aircraft: AOG, in maintenance and unserviceable win, and the sentence stays
among the reasons. Serviceable and serviceable with deferred defects are never asserted on
a time in service the records cannot give. With no hours entered the time is known to be
zero; with no dated log at all the total stands; a query at now is never before the first
log. The evidence pack, the board list, the due routes and the fleet due list carry the
state; `/fleet/due` sorts `unknown` after `overdue` and before the tolerance states.

Writes: an inspection whose hours the tool would derive at such a date is refused with 422
("at_hours_s cannot be derived: ...; state at_hours_s"), because the derived value would be
the invented one; stated hours are accepted, and are not checked against a time in service
that is not known.

### Corrected: an annual inspection counts toward the 100-hour rule

14 CFR 91.409(b) asks for "an annual or 100-hour inspection" within the preceding 100 hours
of time in service. The engine counted only the 100-hour inspection. Now the later of the two
starts the 100-hour interval. `fleet.toml` says so with `satisfied_by = ["annual inspection"]`
on the 100-hour rule, and the note for an aircraft with neither recorded names both. In the
seeded fleet every annual is older than the 100-hour inspection that follows it, so no
seeded state changes.

### Refused: fitting a part that has reached a life limit

A component that has reached any of its life limits on the day it is fitted, exactly or
past it, in hours, cycles or calendar months as `fleet.toml` sets them for its kind, cannot
be installed. The write is refused with 409 and one sentence that names each limit:
"BAT-04A cannot be fitted to SYN-04 at 2026-08-13 08:00 UTC: it has flown 303 cycles, past
its 300-cycle life limit; a life-limited part that has reached its life limit is replaced,
not fitted again". A limit is reached when nothing of it remains: "it has flown
300 cycles, the whole of its 300-cycle life limit", "it has flown 300.0 h, the whole of its
300 h life limit", "its 24-calendar-month life limit ends that same day, 2026-08-31". The
usage is what the records held by the install's date, on every airframe the part had been
fitted to, counted as the due list counts it (14 CFR 43.10). Reproduced before the change:
the seeded fleet itself refitted BAT-04A to SYN-04 at 303 cycles and fitted BAT-04B five
months after its calendar life had ended, and the ledger accepted both, as it would have
from any operator.

The ruling on the boundary: 14 CFR 43.10(c) requires a control method that deters "the
installation of the part after it has reached its life limit" (text checked against the
2025 edition on govinfo), so a part with nothing left is refused, not only a part past its
limit; the refusal sentence uses the regulation's words. The board's boundary is another
question, whether a part already fitted may keep flying: there remaining 0 is due soon, not
overdue, and a part fitted one cycle short flies its first cycle as due soon and is past
its limit on the next. A first draft of this change accepted the at-limit install on the
board's boundary; the regulation's words overrule it, and no release carried that draft.

The rule is judged at the entry's own date like every other rule of the write path. So a
back-dated install is accepted when the part was under its limits on that date, although it
may be past one today: the normal life of a part is to cross its limit while fitted, and
there the board reports it, the install stands, and taking the part off is allowed, as is
registering a part past its limit. The re-check of later entries carries the rule: a write
that would leave an already recorded later install over its limit is refused naming that
install. One sentence is amended: a pack one cycle past its limit reads "1 cycle past", not
"1 cycles past".

### Changed: how the synthetic fleet is generated

The seed told a story the ledger now refuses to record, so the story changes; every seeded
entry must be one the write rules accept in order, and a test holds that. BAT-04A still
begins on SYN-01 two cycles short of its limit and crosses it on SYN-01's third flight, on
2026-08-06 between 11:07 and 11:08 UTC; it comes off SYN-01 on 2026-08-13 at 08:00 UTC into
storage, stays registered, and is never fitted again. SYN-01 lends BAT-01B to SYN-04 at that
hour; its cycles from SYN-01 count on SYN-04 and stay under the limit, so the seed still
shows life status travelling between airframes. BAT-04B is in service since 2024-08-15, so
its 24-calendar-month life ends on 2026-08-31 while it is fitted, and SYN-04 is grounded from
2026-09-01 00:00 UTC by that pack alone. No pack crosses its cycle limit on SYN-04.

The hours stated at the annual inspection were 0.0 on every aircraft, next to a later
100-hour inspection stating tens of hours. Now the record before the first log describes one
steady rate of flying, from the 100-hour inspection to the total reached by the first log
and back from there, down to zero: four aircraft state hours at their annual, and SYN-03 and
SYN-05 still state 0.0, where that rate runs out before the annual's date. The hours before
the first log stay dated the day before the earliest inspection, months before the first
log: an inspection that states hours is accepted only once those hours are on record at an
earlier date, so the seed has no later date open to it, and the not-known window of the
previous section is as small as an honest record of this fleet allows (LIMITS.md). The
random draws are unchanged, so every other seeded number is the same as in 0.2.0.

What changes, measured at the scene plan's times: on 2026-08-03 six synthetic aircraft that
read "serviceable" or "serviceable with deferred defects" read not known, each before its
first log; on 2026-08-06 10:00 four do, SYN-01 and SYN-05 having flown by then; SYN-04 reads
not known on 2026-08-13 00:00 (it read unserviceable, by a pack whose life had ended before
it was fitted), serviceable on 2026-08-14 and 2026-08-20 (it read unserviceable), and from
2026-09-01 unserviceable by BAT-04B alone. Every other state at every listed time, and every
state at 2026-10-01, is the same. The published evidence sample for SYN-04 changes with its
records: its ledger hash was `157e5edb68a7c46496967bc798c5330cb87510a97d778fa2f0e7140998f78634`
and is `3515dcb1fc7f1282c73fad1c6fbc9ebe39df61537c5629145a4b55d96f42eabf`.

### Re-recorded: five assistant runs, after the seed change

Five of the nine recorded assistant runs no longer replayed on the changed seed: the two
about SYN-04's due items and the fleet's states (its reasons changed), and the three that
list ledger entries of SYN-01, SYN-04 and SYN-05 (SYN-01's two new entries shift every
later id by one, and the annual hours changed). No recording was edited. Each of the five
was recorded again on 2026-09-28 with the same model and the same weights (qwen3:8b, digest
`500a1f067a9f…`), against this seed, at the same computation date, 2026-10-01. The first
attempt at four of the five failed with a server error while the model was still loading
and produced no answer; each of those four was run once more after the load. So every run
produced exactly one answer, nothing was chosen between answers, and every answer passed
the grounding check. The four other runs replay as they were.

What the answers say now, against what they said: the SYN-04 due run names BAT-04B's
calendar limit passed on 2026-08-31 and no cycle item, where it named two packs; the
not-serviceable run lists SYN-04, SYN-05 and SYN-07 with the same reasons the board gives,
and no longer names SYN-03's deferred defect, which is not "not serviceable"; the SYN-05
work order run gives the same recorder and time; the SYN-01 supersession run says entry #5
superseded entry #4 for the same reason, adding that it corrected the time in service; the
SYN-04 components run names BAT-01B fitted on 2026-08-13 in place of BAT-04A. The three
runs that use the ledger end with the sentence that the workbench does not certify
airworthiness or return to service, and the not-serviceable run now ends with it too.

Retired with the re-recording: the byte-for-byte pin of the run "Which aircraft are not
serviceable, and what stops each one?" (SHA-256 `60bade96…28e8c`), which held that run's file
from before `list_aircraft` was dated (0.2.0), with a replay allowance that supplied the
missing date. Its new recording carries the date like every other, so the allowance is
gone and the replay tests hold every recorded call to exactly the query it recorded; a test
checks that every dated call carries the computation date. The frozen-clock replay at
2028-01-01 covers all nine runs as before.

Changed: `life.engine.time_in_service` returns a value or an Unknown; `DueItem.used` and
`DueItem.remaining` may be Unknown; `DueState` gains `unknown`; `LifeRule.satisfied_by`;
`DueItemOut.used` and `.remaining` may be `{"unknown": ...}`; `life.engine.component_usage`;
`ledger.validate` judges every install against the part's life limits; the seed's entries
(60, from 59) and their ids from SYN-01's group on; five recordings; the version string.
Not changed: the ledger and its file format, every response shape, the flights, the seed
value.

## 0.2.0

### Corrected: a state at `as_of` is built from the entries that had occurred by then

In 0.1.0, a query for the state of an aircraft at a past time used every live ledger entry,
including entries dated after that time. The records the life engine reads were folded once
from the whole ledger, with no time parameter, and the time given as `as_of` only limited
which flights counted and set the calendar arithmetic. The consequences, all reproduced
before this change:

- `/aircraft/{key}/due` and `/fleet/due` at a past `as_of` reported work orders opened after
  it, with a reason that named the later date, and measured inspections done after it. In
  the seeded fleet, SYN-05 was "in maintenance" and SYN-07 "AOG" at every `as_of`, including
  January 2026, months before those work orders were opened in August.
- `/aircraft` ignored `as_of` and always answered for the moment of the request.
- The evidence pack at a past `as_of` listed every entry and flight about the aircraft,
  those dated after `as_of` included, and its ledger hash covered them. Two packs for the
  same aircraft at different dates carried the same hash.
- The assistant's `list_aircraft` tool called `/aircraft` without a date, so an answer at a
  pinned computation date drew the board from the day of the question.

Queries at now were not affected, with one narrow exception. An entry may be dated up to
five minutes ahead of the server clock, the allowance for clock skew; anything later is
refused. 0.1.0 counted such an entry at once. 0.2.0 counts it when its time arrives, so for
those few minutes a query at now no longer shows it. Apart from that window the whole
ledger and the ledger as of now are the same set.

The rule now: liveness (what a correction or retraction has killed) is decided over the whole
ledger first, as known now; then only live entries with `occurred_utc` at or before `as_of`
are folded, and only flights that had started by `as_of` count. This is valid time, not
record time: "what was true at `as_of`, as the ledger stands today". `recorded_utc` is kept
for the other question, which nothing asks yet.

Changed: `ledger.project(entries, as_of)`, `Store.projection(as_of)`, `Store.maintenance(key,
as_of)`, `Store.components(as_of)`; `GET /aircraft?as_of=` and `GET /aircraft/{key}?as_of=`;
the evidence pack's entries, flights, reconciliation, record and components are fixed at its
`as_of`; the `list_aircraft` assistant tool is dated. Not changed: the ledger, every response
shape, every pinned test at 2026-10-01, the published evidence sample (its hash is the same
before and after), and eight of the nine recorded runs.

The recorded run "Which aircraft are not serviceable, and what stops each one?"
(`02-not-serviceable.json`) was made when `list_aircraft` was undated. Its file is unchanged,
byte for byte, and its SHA-256 is pinned in the replay test; on replay the tool now sends
the run's computation date, and the recorded records equal the dated ones, so the recorded
answer stands. A new test replays every recording with the clock frozen at 2028-01-01, so
no recorded call depends on the day the tests run; without the change, run 02 would have
started failing on 2026-12-25.

### Corrected: a write is judged at the entry's own date, and may not break a later entry

Until now a new entry was judged against the ledger as of now, the same whole-ledger fold
the reads used before this release. Reproduced before the change: a state change back-dated
inside a work order's open window was refused with "was closed on" when the order had been
closed later; a close dated before a later state change was accepted, and that state change
then sat on a closed order, visible at any `as_of` after it; a remove dated inside an
installation window that a later remove already closed was accepted, and the later remove
then took the part off an aircraft it was no longer on and was silently ignored by the
fold; a retraction of a remove was accepted while a later install on another aircraft
relied on it, leaving the part on two aircraft at once; hours before the first log set
lower and back-dated before an inspection that stated more hours were accepted; and a
correction dated away from the entry it corrected was accepted, leaving a window between
the two dates in which neither counted.

The rule now: every entry is judged against the records as they stood when it happened,
the fold of the live entries that precede it, with liveness decided over the whole ledger
first as in every read; then every already recorded later entry of the same subject is
judged again with the new one in place, and the write is refused with 409 if one of them
would no longer hold, naming that entry. Nothing is written on a refusal. A correction
keeps the `occurred_utc` of the entry it corrects and takes its place among entries at the
same instant; to move a date, the entry is retracted and a new one recorded. This is valid
time throughout: when an entry was recorded plays no part in any check, and record time is
not queryable through any route or tool.

Writes that change answer:

- Accepted now, refused before: a state change dated inside a work order's open window
  when the order was closed later.
- Refused now (409, naming the entry that would no longer hold), accepted before: a close
  dated before a later state change of the same order; a remove dated inside a window that
  a later remove closes; a retraction of a remove that a later install elsewhere relies on;
  hours before the first log set lower, dated before an inspection stating more hours than
  the aircraft would then have had.
- Refused now for the right reason: a second close dated before the recorded close names
  the entry that would land on a closed order; an open-ended install into a gap before a
  later install elsewhere names that later install. Both were refused before, the first as
  "was closed on" a date after the new entry, the second as installed elsewhere later.
- Refused now (409), accepted before: a correction whose `occurred_utc` differs from the
  entry it corrects. `uasw record ... --supersedes N` without `--at` now takes entry N's
  date; the API needs it stated, and the refusal names it.

Every refusal not listed above keeps its 0.1.0 sentence word for word, and
`tests/test_ledger_refusals.py` asserts each 409 of the write path in full. During 0.2.0
development two of them were damaged and are restored before release: an install dated
before the component's registration read "it was registered on <date> registered", and a
remove of a part not fitted, or fitted elsewhere, lost the aircraft it was asked about. No
release carried either.

Changed: `ledger.validate`, `ledger.project(entries, before=)`, `ledger.project.fold_key`
(a correction sorts at its target's place; the seeded fleet folds the same), the CLI
default date of a correction. Not changed: the ledger and its file format, every response
shape, the nine recorded runs, the published evidence sample's hash. One existing test
recorded an inspection with derived hours and then set the hours before the first log
lower, dated earlier; that order is now refused, and the test enters the hours first.

## 0.1.0

First release.
