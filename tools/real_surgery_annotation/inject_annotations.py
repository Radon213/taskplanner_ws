#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import yaml

from . import EVENT_TOPIC, MANIFEST_TOPIC
from .event_model import (
    canonical_json,
    event_stamp_ns,
    load_jsonl,
    records_for_injection,
    sha256_file,
)
from .rosbag_compat import close_reader, read_next_record
from .validate_annotations import validate_case


Record = tuple[str, bytes, int, str]


def require_complete_annotation(
    manifest: dict[str, Any],
    validation: dict[str, Any],
) -> None:
    proposed_count = int(validation["review_status_counts"].get("proposed", 0))
    if proposed_count:
        raise RuntimeError(
            "annotation review is incomplete: "
            f"{proposed_count} proposed candidate(s) remain"
        )
    completion = manifest.get("annotation_adjudication")
    completion_key = "annotation_adjudication.complete"
    if completion is None:
        completion = manifest.get("human_annotation", {})
        completion_key = "human_annotation.complete"
    if not completion.get("complete", False):
        raise RuntimeError(
            "annotation review is incomplete: "
            f"{completion_key} must be true before GT injection"
        )


def stable_merge_records(
    original_records: Iterable[tuple[str, bytes, int]],
    *,
    manifest_payload: bytes,
    event_payloads: list[tuple[dict[str, Any], bytes]],
) -> Iterator[Record]:
    """Merge records with deterministic ordering at identical timestamps.

    The returned fourth field is a diagnostic origin:
    ``manifest``, ``initial_state``, ``original``, or ``event``.
    """
    yield MANIFEST_TOPIC, manifest_payload, 0, "manifest"

    initial_at_zero: list[tuple[dict[str, Any], bytes]] = []
    pending: list[tuple[dict[str, Any], bytes]] = []
    for event, payload in event_payloads:
        if event["event_type"] == "initial_state" and event_stamp_ns(event) == 0:
            initial_at_zero.append((event, payload))
        else:
            pending.append((event, payload))
    for _, payload in initial_at_zero:
        yield EVENT_TOPIC, payload, 0, "initial_state"

    pending_index = 0
    for topic, payload, timestamp_ns in original_records:
        while (
            pending_index < len(pending)
            and event_stamp_ns(pending[pending_index][0]) < timestamp_ns
        ):
            event, event_payload = pending[pending_index]
            yield (
                EVENT_TOPIC,
                event_payload,
                event_stamp_ns(event),
                "event",
            )
            pending_index += 1
        yield topic, payload, timestamp_ns, "original"

    while pending_index < len(pending):
        event, event_payload = pending[pending_index]
        yield EVENT_TOPIC, event_payload, event_stamp_ns(event), "event"
        pending_index += 1


def _serialize_string(text: str) -> bytes:
    from rclpy.serialization import serialize_message
    from std_msgs.msg import String

    return serialize_message(String(data=text))


def _open_reader(bag_dir: Path):
    import rosbag2_py

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag_dir), storage_id="mcap"),
        rosbag2_py.ConverterOptions("cdr", "cdr"),
    )
    return reader


def _reader_records(reader) -> Iterator[tuple[str, bytes, int]]:
    while reader.has_next():
        yield read_next_record(reader)


def _rename_generated_mcap(staging_dir: Path, case_id: str) -> Path:
    mcap_paths = list(staging_dir.glob("*.mcap"))
    if len(mcap_paths) != 1:
        raise RuntimeError(
            f"expected one generated MCAP in {staging_dir}, found {len(mcap_paths)}"
        )
    target_name = f"{case_id}_annotated.mcap"
    target = staging_dir / target_name
    mcap_paths[0].rename(target)

    metadata_path = staging_dir / "metadata.yaml"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    info = metadata["rosbag2_bagfile_information"]
    info["relative_file_paths"] = [target_name]
    for item in info.get("files", []):
        item["path"] = target_name
    metadata_path.write_text(
        yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return target


def inject(
    *,
    source_bag_dir: Path,
    case_dir: Path,
    schema_path: Path,
    tools_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    validation = validate_case(case_dir, schema_path, tools_path)
    if not validation["ok"]:
        raise RuntimeError(
            "annotation validation failed:\n" + "\n".join(validation["errors"])
        )
    if output_dir.exists():
        raise FileExistsError(
            f"derived output already exists and will not be overwritten: {output_dir}"
        )

    manifest_path = case_dir / "annotation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require_complete_annotation(manifest, validation)
    if source_bag_dir.resolve() != Path(
        manifest["source_bag"]["directory"]
    ).resolve():
        raise RuntimeError("source bag directory does not match annotation manifest")
    source_mcap = source_bag_dir / manifest["source_bag"]["mcap_file"]
    source_metadata = source_bag_dir / "metadata.yaml"
    source_hashes_before = {
        "mcap": sha256_file(source_mcap),
        "metadata": sha256_file(source_metadata),
    }
    expected_source_hashes = {
        "mcap": manifest["source_bag"]["mcap_sha256"],
        "metadata": manifest["source_bag"]["metadata_sha256"],
    }
    if source_hashes_before != expected_source_hashes:
        raise RuntimeError(
            f"source checksum mismatch: expected={expected_source_hashes} "
            f"actual={source_hashes_before}"
        )

    event_path = case_dir / manifest["event_file"]
    all_events = load_jsonl(event_path)
    included_statuses = set(
        manifest["ground_truth_injection"]["included_statuses"]
    )
    events = records_for_injection(all_events, included_statuses)
    events.sort(key=event_stamp_ns)

    injected_manifest = dict(manifest)
    injected_manifest["injection"] = {
        "derived_schema_version": "arpa_h_0704_multimodal_bag_v6_observable_gt",
        "event_count": len(events),
        "event_status_counts": dict(Counter(item["review_status"] for item in events)),
        "event_label_origin_counts": dict(
            Counter(item["label_origin"] for item in events)
        ),
        "event_reviewer_kind_counts": dict(
            Counter(
                item["review"]["reviewer_kind"]
                for item in events
                if item.get("review")
            )
        ),
        "proposed_candidates_excluded": validation["review_status_counts"].get(
            "proposed", 0
        ),
        "evaluation_only": True,
        "runtime_consumers": [],
    }
    manifest_payload = _serialize_string(canonical_json(injected_manifest))
    event_payloads = [
        (event, _serialize_string(canonical_json(event))) for event in events
    ]

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = output_dir.parent / f".{output_dir.name}.staging-{os.getpid()}"
    if staging_dir.exists():
        raise FileExistsError(f"staging directory already exists: {staging_dir}")

    import rosbag2_py

    reader = _open_reader(source_bag_dir)
    writer = rosbag2_py.SequentialWriter()
    try:
        writer.open(
            rosbag2_py.StorageOptions(
                uri=str(staging_dir),
                storage_id="mcap",
                custom_data={
                    "case_id": manifest["case_id"],
                    "schema_version": (
                        "arpa_h_0704_multimodal_bag_v6_observable_gt"
                    ),
                    "source_mcap_sha256": source_hashes_before["mcap"],
                    "ground_truth_policy": "evaluation_only",
                },
            ),
            rosbag2_py.ConverterOptions("cdr", "cdr"),
        )
        source_topics = reader.get_all_topics_and_types()
        for topic_id, metadata in enumerate(source_topics):
            writer.create_topic(
                rosbag2_py.TopicMetadata(
                    id=topic_id,
                    name=metadata.name,
                    type=metadata.type,
                    serialization_format=metadata.serialization_format,
                    offered_qos_profiles=metadata.offered_qos_profiles,
                    type_description_hash=metadata.type_description_hash,
                )
            )
        next_topic_id = len(source_topics)
        for offset, topic in enumerate((MANIFEST_TOPIC, EVENT_TOPIC)):
            writer.create_topic(
                rosbag2_py.TopicMetadata(
                    id=next_topic_id + offset,
                    name=topic,
                    type="std_msgs/msg/String",
                    serialization_format="cdr",
                )
            )

        counts: Counter[str] = Counter()
        origin_counts: Counter[str] = Counter()
        last_timestamp = -1
        for topic, payload, timestamp_ns, origin in stable_merge_records(
            _reader_records(reader),
            manifest_payload=manifest_payload,
            event_payloads=event_payloads,
        ):
            if timestamp_ns < last_timestamp:
                raise RuntimeError(
                    f"non-monotonic timestamp {timestamp_ns} < {last_timestamp}"
                )
            writer.write(topic, payload, timestamp_ns)
            counts[topic] += 1
            origin_counts[origin] += 1
            last_timestamp = timestamp_ns
    except Exception:
        try:
            writer.close()
        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
        raise
    finally:
        close_reader(reader)
    writer.close()

    derived_mcap = _rename_generated_mcap(staging_dir, manifest["case_id"])
    source_hashes_after = {
        "mcap": sha256_file(source_mcap),
        "metadata": sha256_file(source_metadata),
    }
    if source_hashes_after != source_hashes_before:
        shutil.rmtree(staging_dir)
        raise RuntimeError("source bag changed while creating the derived bag")

    build_report = {
        "schema": "taskplanner.observable_gt_injection_report.v1",
        "case_id": manifest["case_id"],
        "source_bag_dir": str(source_bag_dir),
        "derived_bag_dir": str(output_dir),
        "source_hashes_before": source_hashes_before,
        "source_hashes_after": source_hashes_after,
        "source_unchanged": True,
        "message_counts": dict(counts),
        "merge_origin_counts": dict(origin_counts),
        "event_label_origin_counts": dict(
            Counter(item["label_origin"] for item in events)
        ),
        "event_reviewer_kind_counts": dict(
            Counter(
                item["review"]["reviewer_kind"]
                for item in events
                if item.get("review")
            )
        ),
        "included_review_statuses": sorted(included_statuses),
        "excluded_proposed_candidate_count": validation[
            "review_status_counts"
        ].get("proposed", 0),
        "derived_mcap_sha256": sha256_file(derived_mcap),
    }
    (staging_dir / "build_report.json").write_text(
        json.dumps(build_report, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    (staging_dir / "checksums.sha256").write_text(
        f"{build_report['derived_mcap_sha256']}  {derived_mcap.name}\n"
        f"{sha256_file(staging_dir / 'metadata.yaml')}  metadata.yaml\n",
        encoding="utf-8",
    )
    # The staging bag must pass the full byte/timestamp/replay verification
    # before it becomes the visible derived output.
    from .validate_injected_bag import validate_bag

    validation_report = validate_bag(
        source_bag_dir=source_bag_dir,
        derived_bag_dir=staging_dir,
        case_dir=case_dir,
    )
    (staging_dir / "validation_report.json").write_text(
        json.dumps(
            validation_report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if not validation_report["ok"]:
        shutil.rmtree(staging_dir)
        raise RuntimeError(
            "derived staging bag failed preservation validation:\n"
            + "\n".join(validation_report["errors"])
        )
    staging_dir.rename(output_dir)
    return build_report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inject reviewed observable events into a derived MCAP."
    )
    parser.add_argument("--source-bag", type=Path, required=True)
    parser.add_argument("--case-dir", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = inject(
        source_bag_dir=args.source_bag,
        case_dir=args.case_dir,
        schema_path=args.schema,
        tools_path=args.tools,
        output_dir=args.output,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
