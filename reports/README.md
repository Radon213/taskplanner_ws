# Reports

This directory contains validation artifacts and historical review notes.

Current release evidence for `0.1.0`:

- `multi_bundle_runtime_probe_60s_release_0_1_0.json`

Most other JSON files are generated probe outputs from iterative debugging and
are kept only when they were already tracked. New generated JSON/image reports
are ignored by Git.

For new runtime experiments, use `tools/record_simulation_events.py`. It writes
the event JSONL plus a `*.metadata.json` sidecar containing the Git commit,
runtime environment, observed VLM model, observed surgeon-actor model, timing,
and event count. Keep both files together when sharing an experiment report.

The Markdown files in this directory are archived review logs from earlier
design rounds. They are useful for historical traceability, but the current
system reference is:

- `README.md`
- `SYSTEM_STRUCTURE.md`
- `DIGITAL_TWIN_RULES.md`
- `surgical_assist_system_handoff.md`
- `RELEASE_NOTES.md`
