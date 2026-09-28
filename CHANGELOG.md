# Changelog

## 0.3.0 (unreleased)

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

What changes, measured at the scene plan's early times: on 2026-08-03 six synthetic
aircraft that read "serviceable" or "serviceable with deferred defects" read not known,
each before its first log; on 2026-08-06 10:00 four do, SYN-01 and SYN-05 having flown by
then. Not changed: every state at 2026-10-01, the published evidence sample and its hash
(`157e5edb…8634`), the nine recorded assistant runs and their frozen-clock replay, and the
live demo, all dated after every first log; the ledger, its file format and the seed.

Changed: `life.engine.time_in_service` returns a value or an Unknown; `DueItem.used` and
`DueItem.remaining` may be Unknown; `DueState` gains `unknown`; `LifeRule.satisfied_by`;
`DueItemOut.used` and `.remaining` may be `{"unknown": ...}`; the version string stays 0.2.0
until 0.3.0 is released.

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
