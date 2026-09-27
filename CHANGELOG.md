# Changelog

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

Queries at now were never affected: an entry cannot be dated in the future, so the whole
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

Not in this release: time-aware validation of writes. A new entry is still judged against
the ledger as of now, not at its own date; see LIMITS.md.

## 0.1.0

First release.
