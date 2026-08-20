# Retraction upstream snapshot tool

`retraction_upstream_snapshot.py` implements the migration plan's M0 capture
boundary. It is a local, read-only source collector: it has no network, ROS,
Neuromeka SDK import, CAN, or robot-control path.

## Capture

Every source root has an explicit stable label. Files below a named directory
are included; no other directory is searched. Filesystem/drive roots (including
`C:\`) and symlinks are refused. Non-UTF-8/binary files are also refused so a
secret cannot silently bypass inspection.

```bash
python3 tools/retraction_upstream_snapshot.py capture \
  --file throat_notebook=/path/to/ETRI_throat_control_fin.ipynb \
  --directory retraction_server=/path/to/retraction_server \
  --environment python_version='3.11.9' \
  --environment ros_distro=jazzy \
  --environment neuromeka_sdk_version=3.5.0.7 \
  --environment-file pip_freeze=/path/to/pip-freeze.txt \
  --output /new/path/upstream-snapshot-20260820
```

The output must not already exist. `manifest.json` records the source-relative
and logical paths, source path, byte size, nanosecond mtime, raw-source SHA-256,
stored SHA-256, redaction count, and environment provenance. A notebook remains
under `sources/` (byte-exact when it has no secret; otherwise a documented
redacted copy) and gets a code-only export under `notebook_exports/`. Exporting
parses JSON and writes text only; it never executes a cell.

Use `--collect-local-environment` only when the capture host itself is the
environment being documented. `--collect-pip-freeze` runs only
`python -m pip freeze --disable-pip-version-check`; it does not query a package
index. Supplied upstream metadata remains separately labeled from local capture
metadata.

## Verify and compare

```bash
python3 tools/retraction_upstream_snapshot.py verify /path/to/snapshot
python3 tools/retraction_upstream_snapshot.py compare before/ after/ --output delta.json
```

Verification checks all stored sizes/hashes, containment, snapshot identity,
UTF-8 scanability, and unredacted secret patterns. Comparison reports Python and
Notebook functions, constants, configuration leaves, C/C++ functions/constants,
Notebook output-only changes, and fallback file changes. It classifies changes
using the five plan categories plus explicit dependency, redacted,
non-executable, and other categories. Supplied/local environment values and
environment metadata files are compared separately from source files.

## Record an accepted-upstream claim

```bash
python3 tools/retraction_upstream_snapshot.py tag-accepted-upstream SNAPSHOT \
  --recorded-by local-operator \
  --partner-approved-by partner-reviewer \
  --partner-approval-reference signed-review-record-42 \
  --partner-approved-at 2026-08-20T12:00:00+09:00
```

This writes a new, non-overwriting `acceptance.json`. All four evidence fields
are required. Its state is deliberately
`user_supplied_external_approval_claim_unverified_by_tool`; neither the immutable
manifest nor the tool claims that partner approval was independently verified.
