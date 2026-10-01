Recorded assistant runs (JSON). Each names the model tag and weight digest, the recording
date, the fixed computation date, and that it ran on the seeded synthetic fleet. Made with
`uasw ask --record`; replayed by tests/test_assistant_recordings.py on every CI run, and by
tests/test_as_of.py with the clock frozen at 2028-01-01.

A recording is never edited. When the seeded fleet changes so that a run's records no longer
come out the same, the run is recorded again, as a real run, with the same model at the same
computation date, and CHANGELOG.md says which runs and why. In 0.3.0 five runs (01, 02, 06,
07, 08) were recorded again after the seed's battery story changed; the four others are the
originals. In 0.5.0 two runs (02, 07) were recorded again, on 2026-10-01, after the seed stopped
flying grounded aircraft: 02 reads the board rows, whose flight counts changed for SYN-01 and
SYN-05, and 07 reads SYN-01's entries, one of whose statements changed. Each was run once
and kept as it came. Before that, from 0.2.0, run 02 had been kept byte for byte from before the
`list_aircraft` tool was dated, with its hash pinned in the replay test and an allowance for
its undated query; its re-recording retired both.
