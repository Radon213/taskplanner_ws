#!/usr/bin/env python3
"""Serve an append-only visual adjudication UI for open-hand evaluations.

The completed V8 evaluation remains immutable.  This server reads its generated
review index, displays the original CAM4 frame and exact VLM crop, and stores a
reviewer's visual decision in a *separate* append-only JSONL ledger.  It never
opens or modifies the original interaction-event labels.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import mimetypes
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlsplit


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
STATIC_ROOT = Path(__file__).resolve().parent / "web"
DEFAULT_REVIEW_INDEX = (
    REPOSITORY_ROOT
    / "output/prompt_optimization/gesture_recognition/0704_all/20260819-clear-frame-v1"
    / "v8-clear-frame-disagreement-review/review_index.json"
)
DEFAULT_DECISIONS = (
    DEFAULT_REVIEW_INDEX.parent / "visual-review-decisions" / "decisions.jsonl"
)

SESSION_SCHEMA = "taskplanner.gesture_visual_review_session.v1"
DECISION_SCHEMA = "taskplanner.gesture_visual_review_decision.v1"
MAX_NOTE_LENGTH = 1_000
SAMPLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
VALID_EVENT_LABELS = {"open_receive", "not_open_receive"}
VALID_DECISIONS = {"open_hand", "not_open_hand", "ambiguous"}
VALID_OUTCOMES = {"TP", "TN", "FP", "FN"}


class InputError(ValueError):
    """An expected, safe-to-report local input validation error."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _safe_repo_path(root: Path, value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise InputError(f"{label} path가 없습니다.")
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise InputError(f"{label} path가 저장소 범위를 벗어납니다.") from exc
    if not candidate.is_file():
        raise InputError(f"{label} 파일을 찾을 수 없습니다: {candidate}")
    return candidate


def _open_hand_from_event_label(label: str) -> bool:
    if label == "open_receive":
        return True
    if label == "not_open_receive":
        return False
    raise InputError(f"지원하지 않는 기존 이벤트 proxy label: {label}")


def _decision_value(value: str) -> bool | None:
    if value == "open_hand":
        return True
    if value == "not_open_hand":
        return False
    if value == "ambiguous":
        return None
    raise InputError("visual decision은 open_hand, not_open_hand, ambiguous 중 하나여야 합니다.")


def _outcome(actual_label: str, predicted_gesture: str) -> str:
    actual_open = _open_hand_from_event_label(actual_label)
    predicted_open = _open_hand_from_event_label(predicted_gesture)
    if actual_open and predicted_open:
        return "TP"
    if not actual_open and not predicted_open:
        return "TN"
    if actual_open:
        return "FN"
    return "FP"


@dataclass(frozen=True)
class ReviewSample:
    """One fixed visual-review sample, with immutable source fingerprints."""

    index: int
    sample_id: str
    case_id: str
    event_id: str
    frame_idx: int
    time_sec: float
    partition: str
    failure_type: str
    comparison_group: str
    sample_kind: str
    existing_event_proxy_label: str
    vlm_predicted_gesture: str
    raw_model_text: str
    original_path: Path
    vlm_input_path: Path
    original_sha256: str
    vlm_input_sha256: str

    @property
    def existing_event_proxy_open_hand(self) -> bool:
        return _open_hand_from_event_label(self.existing_event_proxy_label)

    @property
    def vlm_predicted_open_hand(self) -> bool:
        return _open_hand_from_event_label(self.vlm_predicted_gesture)

    def as_public_record(self) -> dict[str, object]:
        return {
            "index": self.index,
            "sample_id": self.sample_id,
            "case_id": self.case_id,
            "event_id": self.event_id,
            "frame_idx": self.frame_idx,
            "time_sec": self.time_sec,
            "partition": self.partition,
            "failure_type": self.failure_type,
            "comparison_group": self.comparison_group,
            "sample_kind": self.sample_kind,
            "existing_event_proxy_label": self.existing_event_proxy_label,
            "existing_event_proxy_open_hand": self.existing_event_proxy_open_hand,
            "vlm_predicted_gesture": self.vlm_predicted_gesture,
            "vlm_predicted_open_hand": self.vlm_predicted_open_hand,
            "raw_model_text": self.raw_model_text,
            "original_url": f"/api/asset/{self.sample_id}/original",
            "vlm_input_url": f"/api/asset/{self.sample_id}/vlm_input",
        }


class ReviewCatalog:
    """Validated, local-only catalog and append-only decision ledger."""

    def __init__(
        self,
        *,
        repository_root: Path,
        review_index_path: Path,
        decisions_path: Path,
        seed_decision_paths: Sequence[Path] = (),
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.review_index_path = review_index_path.resolve()
        self.decisions_path = decisions_path.resolve()
        self.seed_decision_paths = tuple(path.resolve() for path in seed_decision_paths)
        if not self.review_index_path.is_file():
            raise InputError(f"review index를 찾을 수 없습니다: {self.review_index_path}")
        if self.decisions_path in self.seed_decision_paths:
            raise InputError("현재 visual decision ledger를 seed ledger로 동시에 사용할 수 없습니다.")
        for seed_path in self.seed_decision_paths:
            if not seed_path.is_file():
                raise InputError(f"seed visual decision ledger를 찾을 수 없습니다: {seed_path}")

        raw_index = self.review_index_path.read_bytes()
        self.review_index_sha256 = hashlib.sha256(raw_index).hexdigest()
        try:
            index_payload = json.loads(raw_index.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InputError(f"review index JSON을 읽을 수 없습니다: {exc}") from exc
        if not isinstance(index_payload, Mapping):
            raise InputError("review index는 JSON object여야 합니다.")
        metadata = index_payload.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise InputError("review index metadata는 JSON object여야 합니다.")
        self.metadata = {
            key: value.strip()
            for key, value in metadata.items()
            if key in {"title", "subtitle", "completion_title"}
            and isinstance(value, str)
            and value.strip()
        }
        entries = index_payload.get("entries")
        if not isinstance(entries, list) or not entries:
            raise InputError("review index에 entries가 없습니다.")

        samples: list[ReviewSample] = []
        ids: set[str] = set()
        for position, entry in enumerate(entries, 1):
            if not isinstance(entry, Mapping):
                raise InputError(f"review index entry {position}가 object가 아닙니다.")
            sample = self._parse_entry(entry, position)
            if sample.sample_id in ids:
                raise InputError(f"중복 sample_id: {sample.sample_id}")
            ids.add(sample.sample_id)
            samples.append(sample)

        self.samples = tuple(samples)
        self.samples_by_id = {sample.sample_id: sample for sample in samples}
        self._write_lock = threading.Lock()

    def _parse_entry(self, entry: Mapping[str, object], position: int) -> ReviewSample:
        def required_string(key: str) -> str:
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                raise InputError(f"review index entry {position}: {key}가 필요합니다.")
            return value.strip()

        sample_id = required_string("sample_id")
        if not SAMPLE_ID_PATTERN.fullmatch(sample_id):
            raise InputError(f"안전하지 않은 sample_id: {sample_id}")
        actual_label = required_string("actual_label")
        predicted_gesture = required_string("predicted_gesture")
        if actual_label not in VALID_EVENT_LABELS:
            raise InputError(f"지원하지 않는 actual_label: {actual_label}")
        if predicted_gesture not in VALID_EVENT_LABELS:
            raise InputError(f"지원하지 않는 predicted_gesture: {predicted_gesture}")
        failure_type = required_string("failure_type")
        if failure_type not in VALID_OUTCOMES:
            raise InputError(f"지원하지 않는 failure_type: {failure_type}")
        if failure_type != _outcome(actual_label, predicted_gesture):
            raise InputError(f"entry {sample_id}의 failure_type이 reference/VLM pair와 다릅니다.")
        comparison_group_value = entry.get("comparison_group")
        derived_group = "agreement" if actual_label == predicted_gesture else "disagreement"
        if comparison_group_value is None:
            comparison_group = derived_group
        elif isinstance(comparison_group_value, str) and comparison_group_value in {
            "agreement",
            "disagreement",
        }:
            comparison_group = str(comparison_group_value)
        else:
            raise InputError(f"entry {sample_id}의 comparison_group이 올바르지 않습니다.")
        if comparison_group != derived_group:
            raise InputError(f"entry {sample_id}의 comparison_group이 reference/VLM pair와 다릅니다.")

        try:
            index = int(entry["index"])
            frame_idx = int(entry["frame_idx"])
            time_sec = float(entry["time_sec"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InputError(f"entry {sample_id}의 index/frame/time 값이 올바르지 않습니다.") from exc
        if index != position or frame_idx < 0 or time_sec < 0:
            raise InputError(f"entry {sample_id}의 index/frame/time 범위가 올바르지 않습니다.")

        original_path = _safe_repo_path(
            self.repository_root, entry.get("original_cam4_image"), label="original CAM4"
        )
        vlm_input_path = _safe_repo_path(
            self.repository_root, entry.get("vlm_input_image"), label="VLM input"
        )
        return ReviewSample(
            index=index,
            sample_id=sample_id,
            case_id=required_string("case_id"),
            event_id=required_string("event_id"),
            frame_idx=frame_idx,
            time_sec=time_sec,
            partition=required_string("partition"),
            failure_type=failure_type,
            comparison_group=comparison_group,
            sample_kind=required_string("sample_kind"),
            existing_event_proxy_label=actual_label,
            vlm_predicted_gesture=predicted_gesture,
            raw_model_text=required_string("raw_model_text"),
            original_path=original_path,
            vlm_input_path=vlm_input_path,
            original_sha256=_sha256(original_path),
            vlm_input_sha256=_sha256(vlm_input_path),
        )

    def _validate_decision_record(
        self,
        record: object,
        *,
        ledger_path: Path,
        line_number: int,
        require_current_index_hash: bool,
    ) -> tuple[str, dict[str, object]]:
        if not isinstance(record, dict) or record.get("schema") != DECISION_SCHEMA:
            raise InputError(
                f"visual decision ledger {ledger_path}:{line_number} schema가 올바르지 않습니다."
            )
        sample_id = record.get("sample_id")
        if not isinstance(sample_id, str) or sample_id not in self.samples_by_id:
            raise InputError(
                f"visual decision ledger {ledger_path}:{line_number} sample_id가 review catalog에 없습니다."
            )
        sample = self.samples_by_id[sample_id]
        decision = record.get("decision")
        if not isinstance(decision, str) or decision not in VALID_DECISIONS:
            raise InputError(
                f"visual decision ledger {ledger_path}:{line_number} decision이 올바르지 않습니다."
            )
        if record.get("visual_open_hand") is not _decision_value(decision):
            raise InputError(
                f"visual decision ledger {ledger_path}:{line_number} visual decision 값이 일치하지 않습니다."
            )
        if not isinstance(record.get("decision_id"), str) or not isinstance(record.get("recorded_at"), str):
            raise InputError(
                f"visual decision ledger {ledger_path}:{line_number} identity/timestamp가 올바르지 않습니다."
            )
        if not isinstance(record.get("note"), str) or len(str(record["note"])) > MAX_NOTE_LENGTH:
            raise InputError(
                f"visual decision ledger {ledger_path}:{line_number} note가 올바르지 않습니다."
            )
        if record.get("existing_event_proxy_label") != sample.existing_event_proxy_label:
            raise InputError(
                f"visual decision ledger {ledger_path}:{line_number} event proxy가 review index와 다릅니다."
            )
        if record.get("vlm_predicted_gesture") != sample.vlm_predicted_gesture:
            raise InputError(
                f"visual decision ledger {ledger_path}:{line_number} VLM output이 review index와 다릅니다."
            )
        source = record.get("source")
        if not isinstance(source, Mapping):
            raise InputError(
                f"visual decision ledger {ledger_path}:{line_number} source가 올바르지 않습니다."
            )
        source_index_hash = source.get("review_index_sha256")
        if not isinstance(source_index_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_index_hash):
            raise InputError(
                f"visual decision ledger {ledger_path}:{line_number} review index hash가 올바르지 않습니다."
            )
        if require_current_index_hash and source_index_hash != self.review_index_sha256:
            raise InputError(
                f"visual decision ledger {ledger_path}:{line_number} review index hash가 현재 queue와 다릅니다."
            )
        if source.get("original_cam4_sha256") != sample.original_sha256:
            raise InputError(
                f"visual decision ledger {ledger_path}:{line_number} original CAM4 hash가 다릅니다."
            )
        if source.get("vlm_input_image_sha256") != sample.vlm_input_sha256:
            raise InputError(
                f"visual decision ledger {ledger_path}:{line_number} VLM input hash가 다릅니다."
            )
        return sample_id, record

    def _load_latest_decisions_from(
        self, ledger_path: Path, *, require_current_index_hash: bool
    ) -> dict[str, dict[str, object]]:
        if not ledger_path.exists():
            return {}
        latest: dict[str, dict[str, object]] = {}
        with ledger_path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise InputError(
                        f"visual decision ledger {ledger_path}:{line_number} JSON 오류: {exc}"
                    ) from exc
                sample_id, record = self._validate_decision_record(
                    record,
                    ledger_path=ledger_path,
                    line_number=line_number,
                    require_current_index_hash=require_current_index_hash,
                )
                latest[sample_id] = record
        return latest

    def _all_latest_decision_states(self) -> dict[str, tuple[dict[str, object], str]]:
        combined: dict[str, tuple[dict[str, object], str]] = {}
        for seed_path in self.seed_decision_paths:
            for sample_id, record in self._load_latest_decisions_from(
                seed_path, require_current_index_hash=False
            ).items():
                combined[sample_id] = (record, "seed")
        for sample_id, record in self._load_latest_decisions_from(
            self.decisions_path, require_current_index_hash=True
        ).items():
            combined[sample_id] = (record, "current")
        return combined

    @staticmethod
    def _public_decision(record: Mapping[str, object], *, origin: str) -> dict[str, object]:
        return {
            "decision_id": record["decision_id"],
            "recorded_at": record["recorded_at"],
            "decision": record["decision"],
            "visual_open_hand": record["visual_open_hand"],
            "note": record["note"],
            "supersedes_decision_id": record.get("supersedes_decision_id"),
            "origin": origin,
        }

    def session(self) -> dict[str, object]:
        latest = self._all_latest_decision_states()
        public_decisions = {
            sample_id: self._public_decision(record, origin=origin)
            for sample_id, (record, origin) in latest.items()
        }
        return {
            "schema": SESSION_SCHEMA,
            "review_index_sha256": self.review_index_sha256,
            "sample_count": len(self.samples),
            "reviewed_count": len(public_decisions),
            "unreviewed_count": len(self.samples) - len(public_decisions),
            "samples": [sample.as_public_record() for sample in self.samples],
            "decisions": public_decisions,
            "metadata": self.metadata,
            "policy": {
                "original_event_labels_mutable": False,
                "decision_storage": "separate_append_only_visual_review_ledger",
                "review_target": "visible open gloved hand held out by upper-right surgeon",
                "seed_decisions_read_only": bool(self.seed_decision_paths),
            },
        }

    def append_decision(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, Mapping):
            raise InputError("decision request는 JSON object여야 합니다.")
        sample_id = payload.get("sample_id")
        if not isinstance(sample_id, str) or sample_id not in self.samples_by_id:
            raise InputError("알 수 없는 sample_id입니다.")
        decision = payload.get("decision")
        if not isinstance(decision, str) or decision not in VALID_DECISIONS:
            raise InputError("유효하지 않은 visual decision입니다.")
        note_value = payload.get("note", "")
        if not isinstance(note_value, str):
            raise InputError("note는 문자열이어야 합니다.")
        note = note_value.strip()
        if len(note) > MAX_NOTE_LENGTH:
            raise InputError(f"note는 {MAX_NOTE_LENGTH}자 이하여야 합니다.")

        sample = self.samples_by_id[sample_id]
        with self._write_lock:
            previous_state = self._all_latest_decision_states().get(sample_id)
            previous = previous_state[0] if previous_state is not None else None
            record: dict[str, object] = {
                "schema": DECISION_SCHEMA,
                "decision_id": str(uuid.uuid4()),
                "recorded_at": _utc_now(),
                "sample_id": sample.sample_id,
                "decision": decision,
                "visual_open_hand": _decision_value(decision),
                "note": note,
                "existing_event_proxy_label": sample.existing_event_proxy_label,
                "vlm_predicted_gesture": sample.vlm_predicted_gesture,
                "source": {
                    "review_index_sha256": self.review_index_sha256,
                    "original_cam4_sha256": sample.original_sha256,
                    "vlm_input_image_sha256": sample.vlm_input_sha256,
                },
            }
            if previous is not None:
                record["supersedes_decision_id"] = previous["decision_id"]

            self.decisions_path.parent.mkdir(parents=True, exist_ok=True)
            with self.decisions_path.open("a", encoding="utf-8") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return self._public_decision(record, origin="current")

    def asset(self, sample_id: str, kind: str) -> Path:
        sample = self.samples_by_id.get(sample_id)
        if sample is None:
            raise InputError("알 수 없는 sample_id입니다.")
        if kind == "original":
            return sample.original_path
        if kind == "vlm_input":
            return sample.vlm_input_path
        raise InputError("알 수 없는 asset 종류입니다.")


class GestureReviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], catalog: ReviewCatalog) -> None:
        self.catalog = catalog
        super().__init__(server_address, GestureReviewRequestHandler)


class GestureReviewRequestHandler(BaseHTTPRequestHandler):
    server: GestureReviewHTTPServer
    server_version = "TaskplannerGestureVisualReview/1.0"

    def _send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'self'",
        )

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"ok": False, "error": message}, status)

    def _serve_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_error(HTTPStatus.NOT_FOUND, "파일을 읽을 수 없습니다.")
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        try:
            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Cache-Control", "no-store")
                self._send_security_headers()
                self.end_headers()
                return
            if path == "/api/health":
                self._send_json(
                    {
                        "ok": True,
                        "sample_count": len(self.server.catalog.samples),
                        "decisions_path": str(self.server.catalog.decisions_path),
                    }
                )
                return
            if path == "/api/session":
                self._send_json(self.server.catalog.session())
                return
            if path.startswith("/api/asset/"):
                parts = [unquote(part) for part in path.split("/")]
                if len(parts) != 5 or parts[:3] != ["", "api", "asset"]:
                    self._send_error(HTTPStatus.NOT_FOUND, "asset 경로가 올바르지 않습니다.")
                    return
                self._serve_file(self.server.catalog.asset(parts[3], parts[4]))
                return
            static_name = "index.html" if path in {"/", "/index.html"} else path.lstrip("/")
            if static_name not in {"index.html", "app.js", "styles.css"}:
                self._send_error(HTTPStatus.NOT_FOUND, "지원하지 않는 경로입니다.")
                return
            self._serve_file(STATIC_ROOT / static_name)
        except InputError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/api/decision":
            self._send_error(HTTPStatus.NOT_FOUND, "지원하지 않는 경로입니다.")
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > 8_192:
                raise InputError("decision request 크기가 올바르지 않습니다.")
            raw = self.rfile.read(content_length)
            payload = json.loads(raw.decode("utf-8"))
            decision = self.server.catalog.append_decision(payload)
            self._send_json({"ok": True, "decision": decision}, HTTPStatus.CREATED)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, f"JSON request를 읽을 수 없습니다: {exc}")
        except InputError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

    def log_message(self, format: str, *args: object) -> None:
        # Avoid writing user-entered notes to the journal; retain request status only.
        self.log_date_time_string()
        return


def build_catalog(
    *,
    review_index_path: Path = DEFAULT_REVIEW_INDEX,
    decisions_path: Path = DEFAULT_DECISIONS,
    seed_decision_paths: Sequence[Path] = (),
) -> ReviewCatalog:
    return ReviewCatalog(
        repository_root=REPOSITORY_ROOT,
        review_index_path=review_index_path,
        decisions_path=decisions_path,
        seed_decision_paths=seed_decision_paths,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1", help="loopback address to bind")
    parser.add_argument("--port", type=int, default=8891, help="HTTP port")
    parser.add_argument(
        "--review-index", type=Path, default=DEFAULT_REVIEW_INDEX, help="immutable review index"
    )
    parser.add_argument(
        "--decisions", type=Path, default=DEFAULT_DECISIONS, help="append-only visual decision JSONL"
    )
    parser.add_argument(
        "--seed-decisions",
        type=Path,
        action="append",
        default=[],
        help="read-only existing visual decision JSONL to inherit into this queue",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.bind not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("이 검토 UI는 local loopback bind만 허용합니다.")
    if not 1 <= args.port <= 65_535:
        raise SystemExit("port는 1부터 65535 사이여야 합니다.")
    catalog = build_catalog(
        review_index_path=args.review_index.resolve(),
        decisions_path=args.decisions.resolve(),
        seed_decision_paths=tuple(path.resolve() for path in args.seed_decisions),
    )
    with GestureReviewHTTPServer((args.bind, args.port), catalog) as server:
        print(f"Gesture visual review UI: http://{args.bind}:{args.port}/", flush=True)
        print(f"Review index SHA-256: {catalog.review_index_sha256}", flush=True)
        print(f"Decision ledger: {catalog.decisions_path}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
