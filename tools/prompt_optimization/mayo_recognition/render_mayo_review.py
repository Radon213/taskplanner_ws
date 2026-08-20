#!/usr/bin/env python3
"""Render original CAM4 evidence for an already-completed Mayo evaluation.

This is an offline review aid. It reads a result JSON after inference and may
write the reference and prediction onto review images, but those annotations
are never part of a future NInfer request.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import mayo_prompt_eval as evaluator


def _failure_reasons(record: dict[str, Any]) -> list[str]:
    """Classify post-inference review candidates without changing a score."""

    score = record.get("score") if isinstance(record.get("score"), dict) else {}
    input_record = record.get("input") if isinstance(record.get("input"), dict) else {}
    reasons: list[str] = []
    if record.get("request_error"):
        reasons.append("transport")
    if score.get("valid_json") is False:
        reasons.append("invalid-json")
    if score.get("contract_valid") is False:
        reasons.append("contract")
    mode = input_record.get("mode")
    if mode == "crop" and score.get("correct") is False:
        reasons.append("wrong-crop-class")
    elif mode == "inventory" and score.get("exact") is False:
        reasons.append("inventory-mismatch")
    elif mode == "arrival" and score.get("exact") is False:
        reasons.append("arrival-mismatch")
    return reasons


def _text_lines(record: dict[str, Any]) -> list[str]:
    score = record.get("score") if isinstance(record.get("score"), dict) else {}
    expected = record.get("evaluation_reference")
    predicted = score.get("predicted")
    failures = _failure_reasons(record)
    status = "ok" if not failures else "failure: " + ", ".join(failures)
    return [
        f"sample: {record.get('input', {}).get('sample_id', '')}",
        f"expected (evaluation only): {expected}",
        f"predicted: {predicted}",
        status,
    ]


def _draw_lines(image, lines: list[str]):
    import cv2

    output = image.copy()
    y = 26
    for line in lines:
        cv2.putText(output, line, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(output, line, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 1, cv2.LINE_AA)
        y += 28
    return output


def _decode_image(image_bytes: bytes):
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise evaluator.EvaluationError("could not decode CAM4 JPEG for review")
    return image


def _sample_map(events_path: Path) -> dict[str, evaluator.Sample]:
    events = evaluator.load_events(events_path)
    samples = evaluator.make_calibration_samples(events) + evaluator.make_frozen_arrival_samples(events)
    return {sample.sample_id: sample for sample in samples}


def _manifest_identity(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only deterministic pixel-transform fields for review verification."""

    selected: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise evaluator.EvaluationError("image manifest contains a non-object")
        selected.append(
            {
                key: row.get(key)
                for key in ("label", "mime_type", "preprocessor", "source", "normalized", "geometry", "codec")
            }
        )
    return selected


def _normalized_images_for_review(
    *,
    payload: dict[str, Any],
    input_record: dict[str, Any],
    source_images: list[tuple[str, bytes, str]],
) -> list[tuple[str, bytes, str]]:
    image_policy = payload.get("image_policy") if isinstance(payload.get("image_policy"), dict) else {}
    preprocessor = image_policy.get("preprocessor") if isinstance(image_policy.get("preprocessor"), dict) else {}
    requested_flag = preprocessor.get("requested_flag")
    if requested_flag != evaluator.IMAGE_PREPROCESS_LETTERBOX_512_Q95:
        raise evaluator.EvaluationError("normalized review requested for a result without the approved letterbox policy")
    normalized_images, actual_manifest = evaluator.preprocess_images_for_request(
        source_images,
        image_preprocess=requested_flag,
    )
    expected_manifest = input_record.get("image_manifest")
    if not isinstance(expected_manifest, list):
        raise evaluator.EvaluationError("normalized result has no per-image manifest")
    if _manifest_identity(actual_manifest) != _manifest_identity(expected_manifest):
        raise evaluator.EvaluationError("normalized review does not reproduce the recorded image manifest")
    return normalized_images


def _write_failure_sheet(images: list[Any], destination: Path) -> None:
    """Create a compact two-column sheet from already-rendered failed samples."""

    import cv2
    import numpy as np

    if not images:
        return
    cell_width, cell_height, padding, columns = 900, 360, 12, 2
    cells: list[Any] = []
    for image in images:
        source_height, source_width = image.shape[:2]
        scale = min(
            (cell_width - 2 * padding) / source_width,
            (cell_height - 2 * padding) / source_height,
        )
        resized = cv2.resize(
            image,
            (max(1, round(source_width * scale)), max(1, round(source_height * scale))),
            interpolation=cv2.INTER_AREA,
        )
        cell = np.zeros((cell_height, cell_width, 3), dtype=np.uint8)
        top = (cell_height - resized.shape[0]) // 2
        left = (cell_width - resized.shape[1]) // 2
        cell[top : top + resized.shape[0], left : left + resized.shape[1]] = resized
        cells.append(cell)
    rows = (len(cells) + columns - 1) // columns
    sheet = np.zeros((rows * cell_height, columns * cell_width, 3), dtype=np.uint8)
    for index, cell in enumerate(cells):
        row, column = divmod(index, columns)
        top, left = row * cell_height, column * cell_width
        sheet[top : top + cell_height, left : left + cell_width] = cell
    if not cv2.imwrite(str(destination), sheet):
        raise evaluator.EvaluationError(f"could not write failure review sheet: {destination}")


def render(result_path: Path, output_dir: Path, *, representation: str = "source") -> dict[str, Any]:
    if representation not in {"source", "normalized"}:
        raise evaluator.EvaluationError(f"unsupported review representation: {representation}")
    if output_dir.exists():
        raise evaluator.EvaluationError(f"refusing to overwrite review output: {output_dir}")
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise evaluator.EvaluationError("evaluation result must be a JSON object")
    source = payload.get("source")
    if not isinstance(source, dict):
        raise evaluator.EvaluationError("evaluation result has no source block")
    bag = Path(str(source.get("bag", "")))
    events = Path(str(source.get("event_reference", "")))
    records = payload.get("records")
    if not isinstance(records, list):
        raise evaluator.EvaluationError("evaluation result has no records")
    samples = _sample_map(events)
    requested_indices = {
        index
        for record in records
        if isinstance(record, dict)
        for index in record.get("input", {}).get("frame_indices", [])
        if isinstance(index, int)
    }
    frames = evaluator._decode_cam4_frames(bag, requested_indices)
    output_dir.mkdir(parents=True)
    manifest_records: list[dict[str, Any]] = []
    failure_images: list[Any] = []
    for ordinal, record in enumerate(records, 1):
        if not isinstance(record, dict):
            continue
        input_record = record.get("input")
        if not isinstance(input_record, dict):
            continue
        sample_id = str(input_record.get("sample_id", ""))
        sample = samples.get(sample_id)
        if sample is None:
            raise evaluator.EvaluationError(f"review sample no longer resolves: {sample_id}")
        source_images_for_sample = evaluator.images_for(sample, frames)
        images = (
            source_images_for_sample
            if representation == "source"
            else _normalized_images_for_review(
                payload=payload,
                input_record=input_record,
                source_images=source_images_for_sample,
            )
        )
        source_images: list[dict[str, str]] = []
        for label, image_bytes, _mime in images:
            source_name = f"{representation}_{ordinal:02d}_{sample_id.replace('/', '_')}_{label}.jpg"
            source_path = output_dir / source_name
            source_path.write_bytes(image_bytes)
            source_images.append({"label": label, "image": source_name})
        decoded = [_decode_image(image_bytes) for _label, image_bytes, _mime in images]
        if len(decoded) == 2:
            import cv2

            height = min(decoded[0].shape[0], decoded[1].shape[0])
            left = cv2.resize(decoded[0], (int(decoded[0].shape[1] * height / decoded[0].shape[0]), height))
            right = cv2.resize(decoded[1], (int(decoded[1].shape[1] * height / decoded[1].shape[0]), height))
            image = cv2.hconcat([left, right])
        else:
            image = decoded[0]
        reviewed = _draw_lines(image, _text_lines(record))
        safe_name = f"{ordinal:02d}_{sample_id.replace('/', '_')}.jpg"
        destination = output_dir / safe_name
        raw_destination = output_dir / f"raw_{safe_name}"
        import cv2

        if not cv2.imwrite(str(raw_destination), image):
            raise evaluator.EvaluationError(f"could not write raw review image: {raw_destination}")
        if not cv2.imwrite(str(destination), reviewed):
            raise evaluator.EvaluationError(f"could not write review image: {destination}")
        failures = _failure_reasons(record)
        if failures:
            failure_images.append(reviewed)
        manifest_records.append(
            {
                "sample_id": sample_id,
                "frame_indices": list(sample.frame_indices),
                "image": destination.name,
                "raw_image": raw_destination.name,
                "source_images": source_images,
                "representation": representation,
                "evaluation_reference": record.get("evaluation_reference"),
                "score": record.get("score"),
                "failure_reasons": failures,
            }
        )
    failure_sheet = ""
    if failure_images:
        failure_sheet = "failure_review_sheet.jpg"
        _write_failure_sheet(failure_images, output_dir / failure_sheet)
    manifest = {
        "schema": "taskplanner.mayo_visual_review.v1",
        "evaluation_result": str(result_path),
        "records": manifest_records,
        "failure_review_sheet": failure_sheet,
        "failure_record_count": len(failure_images),
        "representation": representation,
        "boundary": "rendered after inference; not valid model input",
    }
    (output_dir / "review_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"output_dir": str(output_dir), "rendered": len(manifest_records)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--representation", choices=("source", "normalized"), default="source")
    args = parser.parse_args(argv)
    try:
        result = render(args.result, args.output_dir, representation=args.representation)
    except (evaluator.EvaluationError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
