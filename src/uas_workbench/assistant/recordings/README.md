Recorded assistant runs (JSON). Each names the model tag and weight digest, the recording
date, the fixed computation date, and that it ran on the seeded synthetic fleet. Made with
`uasw ask --record`; replayed by tests/test_assistant_recordings.py on every CI run.

Migration in 0.2.0. Before 0.2.0 the `list_aircraft` tool called `/aircraft` with no date, so
the board it returned was computed on the day of the recording, not at the run's computation
date. Since 0.2.0 the tool sends the computation date like every other dated tool. One run
was recorded before that change, `02-not-serviceable.json`; it is kept byte for byte, as
evidence is, and its SHA-256 is pinned in the replay test. On replay the tool now sends the
date, and the recorded records must still equal the dated ones. They do: the answer was
consistent with its computation date all along. No recording was re-made or edited.
