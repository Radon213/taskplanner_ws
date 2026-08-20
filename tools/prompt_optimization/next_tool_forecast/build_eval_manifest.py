#!/usr/bin/env python3
"""Build a causal, GT-separated next-tool forecast benchmark from 0704.

The two output JSONL files are intentionally separated:

* ``inputs.jsonl`` contains only causal public inputs plus local media bindings.
* ``labels.jsonl`` contains the future transfer outcome used only after inference.

The runner never sends a label row, case identifier, absolute timestamp, source
path, or annotation metadata to the model.  This is an offline prompt experiment
and does not alter the production VLM/BT/DT path.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import random
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

from prompt_contract import HORIZON_SEC, LOOKBACK_SEC, MODEL_ID, SCHEMA_VERSION, TOOL_ID_SET


TASK_DIR = Path(__file__).resolve().parent
REPO_ROOT = TASK_DIR.parents[2]
RUNS_ROOT = TASK_DIR / "runs"
# ``0704_5`` is deliberately absent here.  It is included in the separate
# coverage audit, but does not have a complete causally bounded reference or
# paired proxy manifest and therefore cannot be silently converted into a
# negative example.
AUDIT_CASES = tuple(f"0704_{number}" for number in range(5, 18))
BENCHMARK_CASES = tuple(f"0704_{number}" for number in range(6, 18))
FINAL_HOLDOUT_CASES = frozenset({"0704_15", "0704_16", "0704_17"})
DEVELOPMENT_CASES = tuple(case_id for case_id in BENCHMARK_CASES if case_id not in FINAL_HOLDOUT_CASES)

# A development case is divided in chronological order.  The calibration
# labels must end before the boundary, while the challenge's *first image*
# must begin after it.  The four-second margin on each side produces an
# eight-second no-touch zone even after the six-second image lookback and
# eight-second forecast horizon are considered.
TEMPORAL_BOUNDARY_FRACTION = 0.50
TEMPORAL_EMBARGO_SEC = 4.0
EPSILON = 1e-6
INPUT_SCHEMA = "taskplanner.next_tool_forecast_input.v1"
LABEL_SCHEMA = "taskplanner.next_tool_forecast_label.v1"
AUDIT_SCHEMA = "taskplanner.next_tool_forecast_audit.v1"
ASR_INPUT_FORMATS = ("plain", "timestamped_relative")


class BenchmarkError(RuntimeError):
    """Fail closed when a source, time boundary, or media mapping is unsafe."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f"JSON object required: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise BenchmarkError(f"JSON object required: {path}:{line_number}")
                rows.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"cannot read JSONL {path}: {exc}") from exc
    return rows


def resolve_bound_file(
    *,
    base: Path,
    relative: Any,
    expected_sha256: Any,
    label: str,
) -> tuple[Path, str]:
    if not isinstance(relative, str) or not relative:
        raise BenchmarkError(f"{label}: missing file binding")
    candidate = Path(relative)
    if candidate.is_absolute():
        raise BenchmarkError(f"{label}: source path must be repository-relative")
    path = (base / candidate).resolve()
    root = REPO_ROOT.resolve()
    if path != root and root not in path.parents:
        raise BenchmarkError(f"{label}: source path escapes workspace")
    if not path.is_file():
        raise BenchmarkError(f"{label}: source file is missing: {path}")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise BenchmarkError(f"{label}: missing SHA-256 binding")
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise BenchmarkError(f"{label}: SHA-256 mismatch")
    return path, actual


def temporal_split_metadata(timestamps: list[float]) -> dict[str, float]:
    if not timestamps:
        raise BenchmarkError("timeline has no timestamps")
    start_sec, end_sec = timestamps[0], timestamps[-1]
    boundary_sec = start_sec + (end_sec - start_sec) * TEMPORAL_BOUNDARY_FRACTION
    return {
        "start_sec": round(start_sec, 9),
        "end_sec": round(end_sec, 9),
        "boundary_sec": round(boundary_sec, 9),
        "calibration_latest_cutoff_sec": round(
            boundary_sec - TEMPORAL_EMBARGO_SEC - HORIZON_SEC[1], 9
        ),
        "challenge_earliest_cutoff_sec": round(
            boundary_sec + TEMPORAL_EMBARGO_SEC + LOOKBACK_SEC, 9
        ),
        "embargo_sec_each_side": TEMPORAL_EMBARGO_SEC,
    }


def case_split(case_id: str, cutoff_sec: float, timestamps: list[float]) -> str:
    """Assign a row without letting calibration labels touch challenge input.

    ``final_holdout`` is strictly case-disjoint.  Within the development cases,
    a sample is usable for calibration only when its full future label window
    ends before the central embargo, and it is usable for challenge only when
    its full visual lookback starts after that embargo.  Everything in between
    remains unscored rather than being assigned opportunistically.
    """

    if case_id in FINAL_HOLDOUT_CASES:
        return "final_holdout"
    if case_id not in DEVELOPMENT_CASES:
        raise BenchmarkError(f"{case_id}: not an eligible benchmark case")
    boundary_sec = temporal_split_metadata(timestamps)["boundary_sec"]
    if cutoff_sec + HORIZON_SEC[1] <= boundary_sec - TEMPORAL_EMBARGO_SEC + EPSILON:
        return "development_calibration"
    if cutoff_sec - LOOKBACK_SEC >= boundary_sec + TEMPORAL_EMBARGO_SEC - EPSILON:
        return "development_challenge"
    return "development_embargoed"


PARTITION_SPLITS = {
    "development_calibration": frozenset({"development_calibration"}),
    "development_challenge": frozenset({"development_challenge"}),
    "development_all": frozenset({"development_calibration", "development_challenge"}),
    "final_holdout": frozenset({"final_holdout"}),
    "all_eligible": frozenset(
        {"development_calibration", "development_challenge", "final_holdout"}
    ),
}


def parse_cases(value: str) -> tuple[str, ...]:
    requested = tuple(item.strip() for item in value.split(",") if item.strip())
    if not requested:
        raise argparse.ArgumentTypeError("at least one case is required")
    unknown = sorted(set(requested) - set(BENCHMARK_CASES))
    if unknown:
        raise argparse.ArgumentTypeError(f"unsupported case IDs: {', '.join(unknown)}")
    return tuple(sorted(set(requested), key=lambda item: int(item.removeprefix("0704_"))))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--partition",
        choices=tuple(PARTITION_SPLITS),
        default="development_calibration",
        help=(
            "development_calibration/challenge are temporal partitions of 0704_6-14; "
            "final_holdout is case-disjoint 0704_15-17."
        ),
    )
    parser.add_argument("--cases", type=parse_cases, default=None)
    parser.add_argument(
        "--proxy-root",
        type=Path,
        default=Path.home() / ".cache/taskplanner_annotation",
    )
    parser.add_argument("--lead-sec", type=float, default=4.0)
    parser.add_argument("--negative-stride-sec", type=float, default=6.0)
    parser.add_argument("--negative-ratio", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=20260818)
    parser.add_argument(
        "--asr-input-format",
        choices=ASR_INPUT_FORMATS,
        default="plain",
        help=(
            "plain sends causal transcript text only; timestamped_relative sends "
            "each causal text with its negative relative availability offset."
        ),
    )
    parser.add_argument("--verify-proxy-sha256", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.cases is None:
        args.cases = (
            tuple(sorted(FINAL_HOLDOUT_CASES))
            if args.partition == "final_holdout"
            else BENCHMARK_CASES
            if args.partition == "all_eligible"
            else DEVELOPMENT_CASES
        )
    elif args.partition != "all_eligible":
        expected = set(FINAL_HOLDOUT_CASES if args.partition == "final_holdout" else DEVELOPMENT_CASES)
        unexpected = sorted(set(args.cases) - expected)
        if unexpected:
            parser.error(
                f"--cases includes a case outside --partition {args.partition}: {', '.join(unexpected)}"
            )
    return args


def ensure_safe_output_dir(path: Path) -> Path:
    output_dir = path.resolve()
    runs_root = RUNS_ROOT.resolve()
    try:
        output_dir.relative_to(runs_root)
    except ValueError as exc:
        raise BenchmarkError(f"output directory must be under {runs_root}") from exc
    if output_dir == runs_root:
        raise BenchmarkError("output directory must be a run subdirectory")
    return output_dir


def validate_timeline(case_id: str, payload: Mapping[str, Any]) -> tuple[list[float], list[dict[str, Any]]]:
    if payload.get("schema") != "taskplanner.video_frame_timeline.v1":
        raise BenchmarkError(f"{case_id}: unexpected frame timeline schema")
    if payload.get("case_id") != case_id:
        raise BenchmarkError(f"{case_id}: timeline case mismatch")
    raw_timestamps = payload.get("timestamps_sec")
    if not isinstance(raw_timestamps, list) or not raw_timestamps:
        raise BenchmarkError(f"{case_id}: timeline has no timestamps")
    timestamps = [float(value) for value in raw_timestamps]
    if any(not math.isfinite(value) for value in timestamps):
        raise BenchmarkError(f"{case_id}: non-finite timestamp")
    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        raise BenchmarkError(f"{case_id}: non-increasing timestamps")
    if int(payload.get("frame_count", -1)) != len(timestamps):
        raise BenchmarkError(f"{case_id}: timeline frame count mismatch")
    gaps: list[dict[str, Any]] = []
    for raw in payload.get("gaps", []):
        if not isinstance(raw, dict):
            raise BenchmarkError(f"{case_id}: invalid timeline gap")
        before = int(raw.get("before_frame_idx", -1))
        after = int(raw.get("after_frame_idx", -1))
        if not (0 <= before < after < len(timestamps)) or after != before + 1:
            raise BenchmarkError(f"{case_id}: invalid timeline gap geometry")
        if abs(float(raw.get("before_time_sec", math.nan)) - timestamps[before]) > EPSILON:
            raise BenchmarkError(f"{case_id}: gap before-time mismatch")
        if abs(float(raw.get("after_time_sec", math.nan)) - timestamps[after]) > EPSILON:
            raise BenchmarkError(f"{case_id}: gap after-time mismatch")
        gaps.append(dict(raw))
    return timestamps, sorted(gaps, key=lambda item: int(item["before_frame_idx"]))


def media_binding(
    *,
    case_id: str,
    proxy_root: Path,
    frame_count: int,
    verify_sha256: bool,
) -> dict[str, Any]:
    manifest_path = proxy_root / case_id / "review_multiview.manifest.json"
    manifest = read_json(manifest_path)
    if manifest.get("case_id") != case_id:
        raise BenchmarkError(f"{case_id}: proxy manifest case mismatch")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        raise BenchmarkError(f"{case_id}: proxy outputs missing")
    binding: dict[str, Any] = {
        "proxy_manifest": {"path": str(manifest_path), "sha256": sha256_file(manifest_path)},
        "views": {},
    }
    for view in ("flir", "cam4"):
        descriptor = outputs.get(view)
        if not isinstance(descriptor, dict):
            raise BenchmarkError(f"{case_id}: {view} proxy descriptor missing")
        path = Path(str(descriptor.get("path", ""))).resolve()
        if not path.is_file():
            raise BenchmarkError(f"{case_id}: {view} proxy is missing: {path}")
        probe = descriptor.get("media_probe")
        if not isinstance(probe, dict) or int(probe.get("frame_count", -1)) != frame_count:
            raise BenchmarkError(f"{case_id}: {view} proxy frame count does not match timeline")
        expected = str(descriptor.get("sha256", ""))
        if len(expected) != 64:
            raise BenchmarkError(f"{case_id}: {view} proxy SHA-256 missing")
        actual = sha256_file(path) if verify_sha256 else None
        if actual is not None and actual != expected:
            raise BenchmarkError(f"{case_id}: {view} proxy SHA-256 mismatch")
        binding["views"][view] = {
            "path": str(path),
            "sha256": expected,
            "size_bytes": path.stat().st_size,
            "verified_sha256": actual is not None,
        }
    return binding


def segment_bounds(frame_idx: int, frame_count: int, gaps: Iterable[Mapping[str, Any]]) -> tuple[int, int]:
    start, end = 0, frame_count - 1
    for gap in gaps:
        before, after = int(gap["before_frame_idx"]), int(gap["after_frame_idx"])
        if frame_idx <= before:
            end = min(end, before)
            break
        start = max(start, after)
    return start, end


def crosses_gap(start_sec: float, end_sec: float, gaps: Iterable[Mapping[str, Any]]) -> bool:
    return any(
        start_sec <= float(gap["before_time_sec"]) + EPSILON
        and end_sec >= float(gap["after_time_sec"]) - EPSILON
        for gap in gaps
    )


def nearest_at_or_before(timestamps: list[float], target_sec: float, lower: int, upper: int) -> int:
    index = bisect.bisect_right(timestamps, target_sec + EPSILON, lower, upper + 1) - 1
    return min(upper, max(lower, index))


def sample_frames(timestamps: list[float], cutoff_frame: int, gaps: list[dict[str, Any]]) -> tuple[list[int], list[float]]:
    start, _end = segment_bounds(cutoff_frame, len(timestamps), gaps)
    targets = (timestamps[cutoff_frame] - LOOKBACK_SEC, timestamps[cutoff_frame] - LOOKBACK_SEC / 2.0, timestamps[cutoff_frame])
    frames: list[int] = []
    for target in targets:
        frame = nearest_at_or_before(timestamps, target, start, cutoff_frame)
        if not frames or frame > frames[-1]:
            frames.append(frame)
    cursor = cutoff_frame - 1
    while len(frames) < 3 and cursor >= start:
        if cursor not in frames:
            frames.insert(max(0, len(frames) - 1), cursor)
        cursor -= 1
    frames = sorted(set(frames))[-3:]
    if len(frames) < 2:
        raise BenchmarkError("not enough causal frames for an example")
    cutoff_sec = timestamps[cutoff_frame]
    offsets = [round(timestamps[frame] - cutoff_sec, 6) for frame in frames]
    offsets[-1] = 0.0
    return frames, offsets


def all_transfer_rows(events: Iterable[Mapping[str, Any]], case_id: str) -> list[dict[str, Any]]:
    rows = [
        dict(event)
        for event in events
        if event.get("case_id") == case_id
        and event.get("event_type") == "tool_transfer"
        and event.get("from") == "scrub_nurse"
        and event.get("to") == "surgeon"
        and event.get("review_status") == "confirmed"
    ]
    return sorted(rows, key=lambda event: (float(event["time_sec"]), str(event["event_id"])))


def causal_asr(voices: Iterable[Mapping[str, Any]], cutoff_sec: float) -> list[str]:
    # The model only sees text already available at the cutoff.  The last 8 s
    # is enough to retain immediate public context without a whole-case history.
    return [
        str(event.get("text", "")).strip()
        for event in voices
        if float(event.get("available_sec", math.inf)) <= cutoff_sec + EPSILON
        and float(event.get("available_sec", -math.inf)) >= cutoff_sec - 8.0 - EPSILON
        and str(event.get("text", "")).strip()
    ]


def causal_asr_timestamped(
    voices: Iterable[Mapping[str, Any]], cutoff_sec: float
) -> list[dict[str, Any]]:
    """Render causal ASR with only a relative, negative availability offset.

    ``available_sec`` and case/cutoff provenance are used only while building
    this evaluation input.  They are never serialized for the model; the model
    receives the text plus ``available_offset_sec`` in [-8, 0].
    """

    rows = [
        event
        for event in voices
        if float(event.get("available_sec", math.inf)) <= cutoff_sec + EPSILON
        and float(event.get("available_sec", -math.inf)) >= cutoff_sec - 8.0 - EPSILON
        and str(event.get("text", "")).strip()
    ]
    rows.sort(key=lambda event: (float(event["available_sec"]), str(event.get("text", ""))))
    return [
        {
            "text": str(event["text"]).strip(),
            "available_offset_sec": round(float(event["available_sec"]) - cutoff_sec, 6),
        }
        for event in rows
    ]


def request_context(events: Iterable[Mapping[str, Any]], cutoff_sec: float) -> bool:
    return any(
        event.get("event_type") == "implicit_tool_request"
        and event.get("review_status") == "confirmed"
        and float(event.get("time_sec", math.inf)) <= cutoff_sec + EPSILON
        and float(event.get("end_sec", event.get("time_sec", -math.inf))) >= cutoff_sec - EPSILON
        for event in events
    )


def target_voice_context(voices: Iterable[Mapping[str, Any]], cutoff_sec: float, tool_id: str) -> bool:
    aliases = {
        "scalpel": ("scalpel", "메스"),
        "adson_forceps": ("adson", "애드슨", "아드손", "앳슨"),
        "allis_forceps": ("allis", "알리스"),
        "bovie": ("bovie", "보비", "보위", "cautery", "커터리"),
        "army_navy_retractor": ("army navy", "army-navy", "아미 네이비", "아미네이비"),
        "bipolar_forceps": ("bipolar", "바이폴라", "바이포라"),
        "mosquito_forceps": ("mosquito", "모스키토", "모스키또"),
        "kocher_retractor": ("kocher", "코처", "thyroid retractor", "갑상선 견인기"),
        "senn_miller_retractor": ("senn", "센 밀러", "센밀러"),
        "harmonic_shears": ("harmonic", "하모닉"),
        "yankauer_suction": ("yankauer", "yankeur", "양카우어", "얀카우어", "석션"),
    }.get(tool_id, ())
    if not aliases:
        return False
    visible_text = " ".join(
        str(event.get("text", "")).casefold()
        for event in voices
        if float(event.get("available_sec", math.inf)) <= cutoff_sec + EPSILON
    )
    return any(alias.casefold() in visible_text for alias in aliases)


def outcome_regime(events: list[dict[str, Any]], voices: list[dict[str, Any]], cutoff_sec: float, tool_id: str) -> str:
    if request_context(events, cutoff_sec):
        return "request_context"
    if target_voice_context(voices, cutoff_sec, tool_id):
        return "voice_context"
    return "anticipatory"


def first_future_transfer(transfers: Iterable[Mapping[str, Any]], cutoff_sec: float) -> dict[str, Any] | None:
    low, high = HORIZON_SEC
    eligible = [
        dict(event)
        for event in transfers
        if low - EPSILON <= float(event["time_sec"]) - cutoff_sec <= high + EPSILON
    ]
    return min(eligible, key=lambda event: (float(event["time_sec"]), str(event["event_id"]))) if eligible else None


def near_term_transfer(transfers: Iterable[Mapping[str, Any]], cutoff_sec: float) -> bool:
    return any(0.0 < float(event["time_sec"]) - cutoff_sec < HORIZON_SEC[0] - EPSILON for event in transfers)


def build_case_rows(
    *,
    case_id: str,
    events: list[dict[str, Any]],
    voices: list[dict[str, Any]],
    timestamps: list[float],
    gaps: list[dict[str, Any]],
    media: dict[str, Any],
    lead_sec: float,
    negative_stride_sec: float,
    negative_ratio: float,
    seed: int,
    asr_input_format: str = "plain",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not HORIZON_SEC[0] <= lead_sec <= HORIZON_SEC[1]:
        raise BenchmarkError("--lead-sec must stay within the forecast horizon")
    if negative_stride_sec <= 0 or negative_ratio < 0:
        raise BenchmarkError("negative stride must be positive and ratio non-negative")
    if asr_input_format not in ASR_INPUT_FORMATS:
        raise BenchmarkError(f"unsupported ASR input format: {asr_input_format}")
    all_transfers = all_transfer_rows(events, case_id)
    transfers = [event for event in all_transfers if str(event.get("tool", "")) in TOOL_ID_SET]
    inputs: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []

    def append_row(
        *,
        example_id: str,
        cutoff_frame: int,
        decision: str,
        tool_id: str,
        target: Mapping[str, Any] | None,
        regime: str,
    ) -> None:
        frames, offsets = sample_frames(timestamps, cutoff_frame, gaps)
        cutoff_sec = timestamps[cutoff_frame]
        split = case_split(case_id, cutoff_sec, timestamps)
        public_asr: list[str] | list[dict[str, Any]] = (
            causal_asr(voices, cutoff_sec)
            if asr_input_format == "plain"
            else causal_asr_timestamped(voices, cutoff_sec)
        )
        input_row = {
            "schema": INPUT_SCHEMA,
            "example_id": example_id,
            "split": split,
            "media": {
                "flir_proxy": media["views"]["flir"]["path"],
                "cam4_proxy": media["views"]["cam4"]["path"],
                "frame_indices": frames,
                "frame_offsets_sec": offsets,
            },
            "public_context": {
                "asr": public_asr,
                "asr_input_format": asr_input_format,
            },
            "provenance": {
                "case_id": case_id,
                "cutoff_frame": cutoff_frame,
                "cutoff_sec": round(cutoff_sec, 9),
                "media_sha256": {
                    "flir": media["views"]["flir"]["sha256"],
                    "cam4": media["views"]["cam4"]["sha256"],
                },
            },
        }
        label_row = {
            "schema": LABEL_SCHEMA,
            "example_id": example_id,
            "split": split,
            "case_id": case_id,
            "target": {
                "decision": decision,
                "tool_id": tool_id,
                "horizon_sec": list(HORIZON_SEC),
                "regime": regime,
                "event_id": str(target["event_id"]) if target else None,
                "event_time_sec": round(float(target["time_sec"]), 9) if target else None,
            },
            "authority": (
                {
                    "kind": "confirmed_observable_transfer",
                    "label_origin": str(target.get("label_origin", "")),
                    "reviewer_kind": str(
                        (target.get("review") or {}).get("reviewer_kind", "")
                    )
                    if isinstance(target.get("review"), dict)
                    else "",
                }
                if target is not None
                else {
                    "kind": "derived_complete_reference_negative",
                    "label_origin": "",
                    "reviewer_kind": "",
                }
            ),
        }
        inputs.append(input_row)
        labels.append(label_row)

    seen_cutoffs: set[int] = set()
    for transfer in transfers:
        transfer_time = float(transfer["time_sec"])
        transfer_frame = int(transfer.get("source_frame_idx", -1))
        if not 0 <= transfer_frame < len(timestamps):
            raise BenchmarkError(f"{case_id}:{transfer.get('event_id')}: invalid transfer frame")
        if abs(timestamps[transfer_frame] - transfer_time) > EPSILON:
            raise BenchmarkError(f"{case_id}:{transfer.get('event_id')}: transfer time/frame mismatch")
        segment_start, _segment_end = segment_bounds(transfer_frame, len(timestamps), gaps)
        preceding_times = [
            float(event["time_sec"])
            for event in all_transfers
            if float(event["time_sec"]) < transfer_time - EPSILON
        ]
        proposed = max(
            timestamps[segment_start],
            transfer_time - lead_sec,
            max(preceding_times, default=-math.inf),
        )
        cutoff_frame = nearest_at_or_before(timestamps, proposed, segment_start, transfer_frame)
        cutoff_sec = timestamps[cutoff_frame]
        if cutoff_frame in seen_cutoffs:
            continue
        delta = transfer_time - cutoff_sec
        if not HORIZON_SEC[0] - EPSILON <= delta <= HORIZON_SEC[1] + EPSILON:
            continue
        if crosses_gap(cutoff_sec - LOOKBACK_SEC, transfer_time, gaps):
            continue
        first = first_future_transfer(all_transfers, cutoff_sec)
        if first is None or str(first["event_id"]) != str(transfer["event_id"]):
            continue
        seen_cutoffs.add(cutoff_frame)
        append_row(
            # The input-side identity is a causal cutoff, not a future target
            # event ID.  The target event remains only in labels.jsonl.
            example_id=f"ntf:{case_id}:cutoff:f{cutoff_frame:06d}",
            cutoff_frame=cutoff_frame,
            decision="handover",
            tool_id=str(transfer["tool"]),
            target=transfer,
            regime=outcome_regime(events, voices, cutoff_sec, str(transfer["tool"])),
        )

    positives = len(inputs)
    target_negative_count = int(math.ceil(positives * negative_ratio))
    rng = random.Random(seed + int(case_id.removeprefix("0704_")))
    candidates: list[int] = []
    cursor = timestamps[0] + LOOKBACK_SEC
    final_time = timestamps[-1]
    while cursor + HORIZON_SEC[1] <= final_time + EPSILON:
        frame = nearest_at_or_before(timestamps, cursor, 0, len(timestamps) - 1)
        cutoff = timestamps[frame]
        if (
            frame not in seen_cutoffs
            and not crosses_gap(cutoff - LOOKBACK_SEC, cutoff + HORIZON_SEC[1], gaps)
            and first_future_transfer(all_transfers, cutoff) is None
            and not near_term_transfer(all_transfers, cutoff)
            and not request_context(events, cutoff)
        ):
            candidates.append(frame)
        cursor += negative_stride_sec
    rng.shuffle(candidates)
    selected = sorted(candidates[:target_negative_count])
    for index, cutoff_frame in enumerate(selected):
        append_row(
            example_id=f"ntf:{case_id}:cutoff:f{cutoff_frame:06d}",
            cutoff_frame=cutoff_frame,
            decision="none",
            tool_id="",
            target=None,
            regime="clean_negative",
        )
    return inputs, labels


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(canonical_json(row) + "\n")


def build_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = ensure_safe_output_dir(args.output_dir)
    if output_dir.exists():
        if not args.overwrite:
            raise BenchmarkError(f"output exists (use --overwrite): {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    all_inputs: list[dict[str, Any]] = []
    all_labels: list[dict[str, Any]] = []
    source_snapshots: dict[str, Any] = {}
    generated_by_split: Counter[str] = Counter()
    try:
        for case_id in args.cases:
            case_dir = REPO_ROOT / "annotations/observable_tool_events/cases" / case_id
            manifest_path = case_dir / "annotation_manifest.json"
            manifest = read_json(manifest_path)
            if manifest.get("case_id") != case_id:
                raise BenchmarkError(f"{case_id}: annotation manifest case mismatch")
            evaluation = manifest.get("evaluation_reference")
            minimal = manifest.get("minimal_interaction_annotation")
            speech = manifest.get("speech_timeline")
            if not isinstance(evaluation, dict) or evaluation.get("complete") is not True:
                raise BenchmarkError(f"{case_id}: complete evaluation reference required")
            if not str(evaluation.get("information_boundary", "")).startswith("evaluation_only"):
                raise BenchmarkError(f"{case_id}: GT information boundary is not evaluation-only")
            if not isinstance(minimal, dict) or not isinstance(speech, dict):
                raise BenchmarkError(f"{case_id}: missing timeline or public ASR binding")
            observed_descriptor = evaluation.get("observed_reference")
            if not isinstance(observed_descriptor, dict):
                raise BenchmarkError(f"{case_id}: observed evaluation reference missing")
            observed_path, observed_hash = resolve_bound_file(
                base=case_dir,
                relative=observed_descriptor.get("file"),
                expected_sha256=observed_descriptor.get("sha256"),
                label=f"{case_id} observed GT",
            )
            timeline_path, timeline_hash = resolve_bound_file(
                base=case_dir,
                relative=minimal.get("timeline_file"),
                expected_sha256=minimal.get("timeline_sha256"),
                label=f"{case_id} frame timeline",
            )
            voice_path, voice_hash = resolve_bound_file(
                base=case_dir,
                relative=speech.get("file"),
                expected_sha256=speech.get("sha256"),
                label=f"{case_id} public ASR",
            )
            timeline_payload = read_json(timeline_path)
            timestamps, gaps = validate_timeline(case_id, timeline_payload)
            media = media_binding(
                case_id=case_id,
                proxy_root=args.proxy_root,
                frame_count=len(timestamps),
                verify_sha256=args.verify_proxy_sha256,
            )
            events = read_jsonl(observed_path)
            voices = read_jsonl(voice_path)
            inputs, labels = build_case_rows(
                case_id=case_id,
                events=events,
                voices=voices,
                timestamps=timestamps,
                gaps=gaps,
                media=media,
                lead_sec=args.lead_sec,
                negative_stride_sec=args.negative_stride_sec,
                negative_ratio=args.negative_ratio,
                seed=args.seed,
                asr_input_format=args.asr_input_format,
            )
            if [row["example_id"] for row in inputs] != [row["example_id"] for row in labels]:
                raise BenchmarkError(f"{case_id}: per-case input/label identity mismatch")
            generated_by_split.update(str(row["split"]) for row in labels)
            selected_splits = PARTITION_SPLITS[args.partition]
            selected_inputs = [row for row in inputs if row["split"] in selected_splits]
            selected_labels = [row for row in labels if row["split"] in selected_splits]
            if [row["example_id"] for row in selected_inputs] != [
                row["example_id"] for row in selected_labels
            ]:
                raise BenchmarkError(f"{case_id}: selected input/label identity mismatch")
            all_inputs.extend(selected_inputs)
            all_labels.extend(selected_labels)
            source_snapshots[case_id] = {
                "annotation_manifest": {"path": str(manifest_path), "sha256": sha256_file(manifest_path)},
                "observed_gt": {"path": str(observed_path), "sha256": observed_hash},
                "timeline": {"path": str(timeline_path), "sha256": timeline_hash},
                "public_asr": {"path": str(voice_path), "sha256": voice_hash},
                "media": media,
                "case_role": (
                    "final_holdout" if case_id in FINAL_HOLDOUT_CASES else "development_temporal"
                ),
                "temporal_partition": (
                    None if case_id in FINAL_HOLDOUT_CASES else temporal_split_metadata(timestamps)
                ),
                "generated_examples_by_split": dict(
                    sorted(Counter(row["split"] for row in labels).items())
                ),
            }

        all_inputs.sort(key=lambda row: (row["split"], row["example_id"]))
        all_labels.sort(key=lambda row: (row["split"], row["example_id"]))
        input_ids = [row["example_id"] for row in all_inputs]
        label_ids = [row["example_id"] for row in all_labels]
        if input_ids != label_ids or len(input_ids) != len(set(input_ids)):
            raise BenchmarkError("input/label identity mismatch")
        input_path = output_dir / "inputs.jsonl"
        label_path = output_dir / "labels.jsonl"
        write_jsonl(input_path, all_inputs)
        write_jsonl(label_path, all_labels)
        by_split = Counter(row["split"] for row in all_labels)
        by_target = Counter(
            f"{row['split']}/{row['target']['decision']}/{row['target']['tool_id'] or 'none'}"
            for row in all_labels
        )
        by_regime = Counter(
            f"{row['split']}/{row['target']['regime']}" for row in all_labels
        )
        by_authority = Counter(
            f"{row['split']}/{row['authority']['kind']}/{row['authority']['label_origin'] or 'none'}"
            for row in all_labels
        )
        audit = {
            "schema": AUDIT_SCHEMA,
            "prompt_contract": SCHEMA_VERSION,
            "model": MODEL_ID,
            "task_definition": {
                "target": "first confirmed scrub_nurse->surgeon transfer within 2-8 seconds after cutoff",
                "negative": "no confirmed transfer in the full 2-8 second window and no near-term transfer under 2 seconds",
                "unresolved_tool_policy": "retractor_bundle_unresolved is excluded from scored classes",
                "media": "three chronological FLIR/CAM4 pairs ending at causal cutoff",
                "asr": (
                    "public transcript rows available at cutoff only"
                    if args.asr_input_format == "plain"
                    else "public transcript text plus only negative relative availability offsets at cutoff"
                ),
            },
            "information_boundary": {
                "gt_use": "labels.jsonl only; never passed to build_messages or NInfer",
                "case_id_and_absolute_time": "stored only for audit/score; never passed to model",
                "timestamped_asr_policy": (
                    "not used"
                    if args.asr_input_format == "plain"
                    else "model receives text and available_offset_sec only; no case ID or absolute timestamp"
                ),
                "production_integration": "none",
            },
            "split_policy": {
                "coverage_audit": list(AUDIT_CASES),
                "eligible_cases": list(BENCHMARK_CASES),
                "excluded_incomplete_case": "0704_5; it is not converted to a negative example",
                "development_cases": list(DEVELOPMENT_CASES),
                "development_temporal_rule": {
                    "calibration": "cutoff + 8s <= central boundary - 4s",
                    "challenge": "cutoff - 6s >= central boundary + 4s",
                    "central_boundary": "50% of each case duration",
                    "unscored_embargo": "all remaining windows",
                },
                "final_case_holdout": sorted(FINAL_HOLDOUT_CASES),
                "claim_limit": "0704_15-17 is an internal case-disjoint holdout, not external generalization",
            },
            "selected_partition": args.partition,
            "parameters": {
                "lead_sec": args.lead_sec,
                "negative_stride_sec": args.negative_stride_sec,
                "negative_ratio": args.negative_ratio,
                "seed": args.seed,
                "asr_input_format": args.asr_input_format,
            },
            "counts": {
                "examples": len(all_inputs),
                "by_split": dict(sorted(by_split.items())),
                "generated_by_split_before_partition_filter": dict(sorted(generated_by_split.items())),
                "by_target": dict(sorted(by_target.items())),
                "by_regime": dict(sorted(by_regime.items())),
                "by_authority": dict(sorted(by_authority.items())),
            },
            "source_snapshots": source_snapshots,
            "files": {
                "inputs.jsonl": sha256_file(input_path),
                "labels.jsonl": sha256_file(label_path),
            },
        }
        audit_path = output_dir / "audit.json"
        audit_path.write_text(json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return {"output_dir": str(output_dir), "audit": audit}
    except Exception:
        # The directory contains evaluation data only, but remove an incomplete
        # generated bundle so it cannot be mistaken for a usable benchmark.
        shutil.rmtree(output_dir, ignore_errors=True)
        raise


def main() -> int:
    args = parse_args()
    try:
        result = build_benchmark(args)
    except BenchmarkError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output_dir": result["output_dir"],
                "examples": result["audit"]["counts"]["examples"],
                "by_split": result["audit"]["counts"]["by_split"],
                "by_regime": result["audit"]["counts"]["by_regime"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
