from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import tools.retraction_upstream_snapshot as snapshot_tool
from tools.retraction_upstream_snapshot import (
    REDACTION_MARKER,
    SnapshotError,
    SourceSpec,
    capture_snapshot,
    compare_snapshots,
    collect_local_environment,
    record_acceptance_tag,
    redact_text,
    verify_snapshot,
)


FIXED_TIME = "2026-08-20T02:22:00Z"
FIXED_MTIME_NS = 1_788_000_000_123_456_789
REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tools" / "retraction_upstream_snapshot.py"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(FIXED_MTIME_NS, FIXED_MTIME_NS))
    return path


def _notebook(
    *,
    output: str,
    code: str | None = None,
    markdown: str = "notes",
    shell_target: str = "/tmp/must-never-run",
) -> str:
    return json.dumps(
        {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {"kernelspec": {"name": "python3"}},
            "cells": [
                {
                    "cell_type": "markdown",
                    "metadata": {},
                    "source": [markdown],
                },
                {
                    "cell_type": "code",
                    "metadata": {},
                    "execution_count": 7,
                    "source": [
                        code
                        or "def retract(distance_m):\n    return distance_m * 1000.0\n"
                    ],
                    "outputs": [{"output_type": "stream", "name": "stdout", "text": [output]}],
                },
                {
                    "cell_type": "code",
                    "metadata": {},
                    "execution_count": None,
                    "source": [f"!touch {shell_target}\n%matplotlib inline\n"],
                    "outputs": [],
                },
            ],
        },
        ensure_ascii=False,
    )


def test_capture_is_allowlisted_redacted_and_exports_notebook_without_execution(tmp_path: Path) -> None:
    sources = tmp_path / "upstream"
    license_value = "super-" + "secret-" + "license"
    metadata_value = "metadata-" + "secret"
    url_password = "build-" + "password"
    config = _write(
        sources / "config.yaml",
        f"robot_ip: 192.168.1.137\nlicense_key: {license_value}\ncustom_gain: 4.5\n",
    )
    sentinel = tmp_path / "must-never-run"
    notebook = _write(
        sources / "control.ipynb",
        _notebook(output="saved output", shell_target=str(sentinel)),
    )
    pip_freeze = _write(
        tmp_path / "pip-freeze.txt",
        f"private-pkg @ https://build-user:{url_password}@example.invalid/pkg.whl\n",
    )
    snapshot = tmp_path / "snapshot"

    manifest = capture_snapshot(
        [
            SourceSpec("config", config, "file"),
            SourceSpec("notebook", notebook, "file"),
        ],
        snapshot,
        supplied_environment={
            "python_version": "3.11.9",
            "ros_distro": "jazzy",
            "neuromeka_sdk_version": "3.5.0.7",
            "api_token": metadata_value,
        },
        environment_files={"pip_freeze": pip_freeze},
        created_at_utc=FIXED_TIME,
    )

    assert manifest["created_at_utc"] == FIXED_TIME
    assert manifest["capture_policy"]["network_or_hardware_access"] == "none"
    assert manifest["environment"]["supplied"]["api_token"] == REDACTION_MARKER
    assert set(manifest["environment"]["required_metadata_status"].values()) == {"recorded"}
    config_entry = next(item for item in manifest["files"] if item["source_label"] == "config")
    assert config_entry["relative_path"] == "config.yaml"
    assert config_entry["size_bytes"] == config.stat().st_size
    assert config_entry["mtime_ns"] == FIXED_MTIME_NS
    assert config_entry["sha256"] == hashlib.sha256(config.read_bytes()).hexdigest()
    assert config_entry["content_transform"] == "secret_redacted"
    assert config_entry["stored_sha256"] != config_entry["sha256"]

    export_entry = manifest["derived_files"][0]
    export = (snapshot / export_entry["snapshot_path"]).read_text(encoding="utf-8")
    assert "def retract(distance_m)" in export
    assert f"# IPython line not executed: !touch {sentinel}" in export
    assert "# IPython line not executed: %matplotlib inline" in export
    assert not sentinel.exists()

    all_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in snapshot.rglob("*")
        if path.is_file()
    )
    assert license_value not in all_text
    assert metadata_value not in all_text
    assert url_password not in all_text
    assert REDACTION_MARKER in all_text
    assert verify_snapshot(snapshot)["ok"] is True


def test_capture_rejects_roots_symlinks_and_destination_inside_source(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError, match="roots may never"):
        capture_snapshot([SourceSpec("root", Path("/"), "directory")], tmp_path / "out")
    with pytest.raises(SnapshotError, match="roots may never"):
        capture_snapshot([SourceSpec("drive", Path("C:\\"), "directory")], tmp_path / "out")

    source_dir = tmp_path / "source"
    _write(source_dir / "code.py", "VALUE = 1\n")
    with pytest.raises(SnapshotError, match="inside an allowlisted directory"):
        capture_snapshot(
            [SourceSpec("source", source_dir, "directory")],
            source_dir / "snapshot",
        )
    link = tmp_path / "linked.py"
    link.symlink_to(source_dir / "code.py")
    with pytest.raises(SnapshotError, match="symlink"):
        capture_snapshot([SourceSpec("link", link, "file")], tmp_path / "linked-snapshot")


def test_snapshot_id_is_stable_across_source_host_paths(tmp_path: Path) -> None:
    first = _write(tmp_path / "host-a" / "code.py", "LIMIT = 50\n")
    second = _write(tmp_path / "host-b" / "code.py", "LIMIT = 50\n")
    first_manifest = capture_snapshot(
        [SourceSpec("code", first, "file")],
        tmp_path / "first",
        supplied_environment={"python_version": "3.11"},
        created_at_utc="2026-01-01T00:00:00Z",
    )
    second_manifest = capture_snapshot(
        [SourceSpec("code", second, "file")],
        tmp_path / "second",
        supplied_environment={"python_version": "3.11"},
        created_at_utc="2026-12-31T00:00:00Z",
    )
    assert first_manifest["snapshot_id"] == second_manifest["snapshot_id"]
    assert first_manifest["files"][0]["source_path"] != second_manifest["files"][0]["source_path"]


def test_compare_reports_functions_constants_config_ros_and_notebook_outputs(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    code = _write(
        sources / "controller.py",
        "JOG_LIMIT = 50\nROS_TOPIC = '/old/topic'\n\ndef retract(value):\n    return value * 1000\n",
    )
    profile = _write(sources / "profile.yaml", "custom_gain: 1.0\nwaypoint: home\n")
    notebook = _write(sources / "control.ipynb", _notebook(output="old output"))
    specs = [
        SourceSpec("code", code, "file"),
        SourceSpec("profile", profile, "file"),
        SourceSpec("notebook", notebook, "file"),
    ]
    capture_snapshot(specs, tmp_path / "before", created_at_utc=FIXED_TIME)

    _write(
        code,
        "JOG_LIMIT = 75\nROS_TOPIC = '/surgery/retraction/command'\n\ndef retract(value):\n    return round(value * 1000)\n",
    )
    _write(profile, "custom_gain: 2.0\nwaypoint: wait\n")
    _write(notebook, _notebook(output="new output"))
    capture_snapshot(specs, tmp_path / "after", created_at_utc=FIXED_TIME)

    report = compare_snapshots(tmp_path / "before", tmp_path / "after")
    keyed = {(item["granularity"], item["symbol"]): item for item in report["changes"]}
    assert keyed[("function", "retract")]["change"] == "modified"
    assert "control_algorithm_change" in keyed[("function", "retract")]["classifications"]
    assert keyed[("constant", "JOG_LIMIT")]["change"] == "modified"
    assert "ros_connection_change" in keyed[("constant", "ROS_TOPIC")]["classifications"]
    assert keyed[("config", "custom_gain")]["change"] == "modified"
    assert "waypoint_gain_sensor_calibration_change" in keyed[("config", "custom_gain")]["classifications"]
    assert keyed[("notebook_output", "<outputs>")]["output_only"] is True
    assert report["summary"]["by_classification"]["notebook_output_cell_change"] == 1


def test_secret_only_delta_is_visible_without_disclosing_value(tmp_path: Path) -> None:
    first_value = "first-" + "license-value"
    second_value = "second-" + "license-value"
    source = _write(tmp_path / "source.env", f"LICENSE_KEY={first_value}\n")
    specs = [SourceSpec("env", source, "file")]
    capture_snapshot(specs, tmp_path / "before", created_at_utc=FIXED_TIME)
    _write(source, f"LICENSE_KEY={second_value}\n")
    capture_snapshot(specs, tmp_path / "after", created_at_utc=FIXED_TIME)

    report = compare_snapshots(tmp_path / "before", tmp_path / "after")
    assert report["changes"][0]["granularity"] == "redacted_value"
    rendered = json.dumps(report)
    assert first_value not in rendered
    assert second_value not in rendered


def test_acceptance_tag_requires_evidence_and_remains_an_unverified_claim(tmp_path: Path) -> None:
    source = _write(tmp_path / "code.py", "VALUE = 1\n")
    snapshot = tmp_path / "snapshot"
    manifest = capture_snapshot(
        [SourceSpec("code", source, "file")],
        snapshot,
        created_at_utc=FIXED_TIME,
    )
    with pytest.raises(SnapshotError, match="requires supplied approval evidence"):
        record_acceptance_tag(
            snapshot,
            recorded_by="operator",
            partner_approved_by="",
            partner_approval_reference="meeting-42",
            partner_approved_at="2026-08-20T12:00:00+09:00",
        )

    record = record_acceptance_tag(
        snapshot,
        recorded_by="operator",
        partner_approved_by="partner-team-reviewer",
        partner_approval_reference="signed-review-record-42",
        partner_approved_at="2026-08-20T12:00:00+09:00",
        recorded_at_utc=FIXED_TIME,
    )
    assert record["tag"] == "accepted-upstream"
    assert record["snapshot_id"] == manifest["snapshot_id"]
    assert record["record_state"] == "user_supplied_external_approval_claim_unverified_by_tool"
    assert record["partner_approval"]["verification"] == "not_verified_by_this_tool"
    assert "approved" not in record
    assert json.loads((snapshot / "manifest.json").read_text())["acceptance"]["state"] == "unreviewed_candidate"
    assert verify_snapshot(snapshot)["ok"] is True
    with pytest.raises(SnapshotError, match="will not be overwritten"):
        record_acceptance_tag(
            snapshot,
            recorded_by="operator",
            partner_approved_by="partner-team-reviewer",
            partner_approval_reference="other",
            partner_approved_at="2026-08-20T12:00:00+09:00",
        )


def test_redaction_covers_bearer_known_tokens_and_dynamic_references() -> None:
    bearer_value = "abcdefghijkl" + "mnop"
    github_value = "ghp_" + "abcdefghijklmnopqrstuvwxyz123456"
    url_password = "pass" + "phrase"
    text = (
        "password = os.getenv('ROBOT_PASSWORD')\n"
        f"Authorization: Bearer {bearer_value}\n"
        f"token={github_value}\n"
        f"url=https://user:{url_password}@example.invalid/path\n"
    )
    redacted, count = redact_text(text)
    assert "os.getenv('ROBOT_PASSWORD')" in redacted
    assert bearer_value not in redacted
    assert github_value not in redacted
    assert url_password not in redacted
    assert count >= 3


def test_quoted_redaction_preserves_python_syntax() -> None:
    secret_value = "literal-" + "secret"
    redacted, count = redact_text(f'API_KEY = "{secret_value}"\n')
    assert redacted == f'API_KEY = "{REDACTION_MARKER}"\n'
    compile(redacted, "redacted.py", "exec")
    assert count == 1


def test_local_environment_uses_only_safe_pip_freeze_and_redacts_output(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = "pkg @ https://user:" + "pip-" + "password@example.invalid/pkg.whl\n"
        stderr = ""

    def fake_run(command: list[str], **kwargs: object) -> Result:
        observed.append(command)
        assert kwargs["timeout"] == 30
        return Result()

    monkeypatch.setattr(snapshot_tool.subprocess, "run", fake_run)
    monkeypatch.setattr(snapshot_tool.importlib.metadata, "version", lambda _name: "3.5.0.7")
    metadata = collect_local_environment(include_pip_freeze=True)
    assert observed == [
        [
            snapshot_tool.sys.executable,
            "-m",
            "pip",
            "freeze",
            "--disable-pip-version-check",
        ]
    ]
    assert metadata["neuromeka_sdk_version"] == "3.5.0.7"
    assert ("pip-" + "password") not in json.dumps(metadata)
    assert REDACTION_MARKER in metadata["pip_freeze"]["stdout"]


def test_invalid_notebook_code_delta_still_gets_a_code_level_report(tmp_path: Path) -> None:
    notebook = _write(
        tmp_path / "source.ipynb",
        _notebook(output="same", code="this is not valid python ???\n"),
    )
    specs = [SourceSpec("notebook", notebook, "file")]
    capture_snapshot(specs, tmp_path / "before", created_at_utc=FIXED_TIME)
    _write(notebook, _notebook(output="same", code="still not valid python !!!\n"))
    capture_snapshot(specs, tmp_path / "after", created_at_utc=FIXED_TIME)

    report = compare_snapshots(tmp_path / "before", tmp_path / "after")
    code_change = next(
        item for item in report["changes"] if item["symbol"] == "<notebook-code>"
    )
    assert code_change["granularity"] == "module_code"
    assert code_change["change"] == "modified"


def test_compare_reports_environment_dependency_changes(tmp_path: Path) -> None:
    source = _write(tmp_path / "code.py", "VALUE = 1\n")
    specs = [SourceSpec("code", source, "file")]
    capture_snapshot(
        specs,
        tmp_path / "before",
        supplied_environment={"python_version": "3.11", "ros_distro": "jazzy"},
        created_at_utc=FIXED_TIME,
    )
    capture_snapshot(
        specs,
        tmp_path / "after",
        supplied_environment={"python_version": "3.12", "ros_distro": "rolling"},
        created_at_utc=FIXED_TIME,
    )
    report = compare_snapshots(tmp_path / "before", tmp_path / "after")
    environment_changes = {
        item["symbol"]: item
        for item in report["changes"]
        if item["granularity"] == "environment"
    }
    assert "supplied.python_version" in environment_changes
    assert environment_changes["supplied.python_version"]["classifications"] == [
        "environment_dependency_change"
    ]
    assert environment_changes["supplied.ros_distro"]["classifications"] == [
        "ros_connection_change"
    ]


def test_verification_fails_closed_on_stored_file_or_manifest_tamper(tmp_path: Path) -> None:
    source = _write(tmp_path / "code.py", "VALUE = 1\n")
    snapshot = tmp_path / "snapshot"
    manifest = capture_snapshot(
        [SourceSpec("code", source, "file")],
        snapshot,
        created_at_utc=FIXED_TIME,
    )
    stored = snapshot / manifest["files"][0]["snapshot_path"]
    stored.write_text("VALUE = 2\n", encoding="utf-8")
    result = verify_snapshot(snapshot)
    assert result["ok"] is False
    assert any("stored sha256 mismatch" in error for error in result["errors"])

    stored.write_text("VALUE = 1\n", encoding="utf-8")
    manifest_path = snapshot / "manifest.json"
    tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_secret = "manifest-" + "secret-leak"
    tampered["environment"]["supplied"]["api_token"] = manifest_secret
    manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
    result = verify_snapshot(snapshot)
    assert result["ok"] is False
    assert any("unredacted secret pattern in manifest.json" in error for error in result["errors"])


def test_cli_capture_verify_compare_and_acceptance_record(tmp_path: Path) -> None:
    source = _write(tmp_path / "code.py", "VALUE = 1\n")
    snapshots = [tmp_path / "first", tmp_path / "second"]
    for destination in snapshots:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(TOOL_PATH),
                "capture",
                "--file",
                f"code={source}",
                "--environment",
                "python_version=3.11",
                "--output",
                str(destination),
            ],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        assert json.loads(result.stdout)["file_count"] == 1

    verify = subprocess.run(
        [sys.executable, "-B", str(TOOL_PATH), "verify", str(snapshots[0])],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(verify.stdout)["ok"] is True

    compare = subprocess.run(
        [
            sys.executable,
            "-B",
            str(TOOL_PATH),
            "compare",
            str(snapshots[0]),
            str(snapshots[1]),
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(compare.stdout)["summary"]["change_count"] == 0

    tag = subprocess.run(
        [
            sys.executable,
            "-B",
            str(TOOL_PATH),
            "tag-accepted-upstream",
            str(snapshots[0]),
            "--recorded-by",
            "local-operator",
            "--partner-approved-by",
            "partner-reviewer",
            "--partner-approval-reference",
            "signed-record-42",
            "--partner-approved-at",
            "2026-08-20T12:00:00+09:00",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(tag.stdout)["record_state"].endswith("unverified_by_tool")
