#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import threading
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .event_model import (
    derive_action,
    load_jsonl,
    load_yaml,
    strip_internal_fields,
)
from .rosbag_compat import close_reader, read_next_record
from .validate_annotations import validate_records


VIEW_TOPICS = {
    "cam4": "/surgery/cam4/color/image/compressed",
    "flir": "/surgery/flir/image/compressed",
}


class AnnotationStore:
    def __init__(
        self,
        *,
        case_dir: Path,
        schema_path: Path,
        tools_path: Path,
        source_bag_dir: Path,
    ) -> None:
        self.case_dir = case_dir
        self.schema_path = schema_path
        self.tools_path = tools_path
        self.source_bag_dir = source_bag_dir
        self.schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.tool_catalog = load_yaml(tools_path)
        self.lock = threading.Lock()

    @property
    def manifest_path(self) -> Path:
        return self.case_dir / "annotation_manifest.json"

    def _load(self) -> tuple[dict[str, Any], list[dict], list[dict]]:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        events = [
            strip_internal_fields(item)
            for item in load_jsonl(self.case_dir / manifest["event_file"])
        ]
        candidates = [
            strip_internal_fields(item)
            for item in load_jsonl(self.case_dir / manifest["candidate_file"])
        ]
        return manifest, events, candidates

    def revision(self) -> str:
        manifest, _, _ = self._load()
        digest = hashlib.sha256()
        for path in (
            self.manifest_path,
            self.case_dir / manifest["event_file"],
            self.case_dir / manifest["candidate_file"],
        ):
            digest.update(path.read_bytes())
        return digest.hexdigest()[:16]

    def state(self) -> dict[str, Any]:
        manifest, events, candidates = self._load()
        return {
            "case_id": manifest["case_id"],
            "duration_sec": manifest["duration_sec"],
            "timeline_origin": manifest["timeline_origin"],
            "human_annotation": manifest["human_annotation"],
            "annotation_adjudication": manifest.get("annotation_adjudication"),
            "review_status_counts": manifest["review_status_counts"],
            "candidates": candidates,
            "events": events,
            "tools": self.tool_catalog["tools"],
            "vocabulary": {
                "event_types": self.schema["properties"]["event_type"]["enum"],
                "holders": self.schema["$defs"]["state"]["properties"]["holder"][
                    "enum"
                ],
                "locations": self.schema["$defs"]["state"]["properties"][
                    "location"
                ]["enum"],
                "visibility": self.schema["properties"]["visibility"]["enum"],
                "views": ["cam4", "flir"],
            },
            "revision": self.revision(),
            "policy": {
                "confirmed_requires_human": True,
                "proposed_is_ground_truth": False,
                "ground_truth_consumers": ["evaluation_only"],
            },
        }

    def _next_event_id(
        self,
        event_type: str,
        events: list[dict],
        candidates: list[dict],
    ) -> str:
        prefix = "I" if event_type == "initial_state" else "E"
        used = {
            item["event_id"]
            for item in events + candidates
            if item["event_id"].startswith(f"{self.case_dir.name}-{prefix}")
        }
        index = 1
        while f"{self.case_dir.name}-{prefix}{index:04d}" in used:
            index += 1
        return f"{self.case_dir.name}-{prefix}{index:04d}"

    @staticmethod
    def _write_jsonl(path: Path, records: list[dict]) -> None:
        text = "".join(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for record in records
        )
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)

    def save_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            expected_revision = str(payload.get("revision", ""))
            if expected_revision != self.revision():
                raise ConflictError(
                    "다른 저장이 먼저 반영되었습니다. 새로고침 후 다시 검토해 주세요."
                )
            reviewer_id = str(payload.get("reviewer_id", "")).strip()
            status = payload.get("review_status")
            if status not in {"confirmed", "ambiguous", "rejected"}:
                raise InputError("검토 결과는 confirmed/ambiguous/rejected 중 하나여야 합니다.")
            if not reviewer_id:
                raise InputError("확정 또는 판정 기록을 위해 검토자 ID를 입력해 주세요.")

            manifest, events, candidates = self._load()
            if manifest.get("annotation_adjudication", {}).get("complete"):
                raise InputError(
                    "최종 승격이 완료된 주석 세트입니다. 후속 수정을 시작하려면 "
                    "annotation_adjudication.complete를 먼저 false로 전환해 "
                    "새 검토 라운드를 명시적으로 열어 주세요."
                )
            event = payload.get("event")
            if not isinstance(event, dict):
                raise InputError("event 객체가 없습니다.")
            event = strip_internal_fields(event)
            event_id = str(event.get("event_id", ""))
            is_new = not event_id or event_id == "NEW"
            if is_new:
                event_id = self._next_event_id(
                    str(event.get("event_type")),
                    events,
                    candidates,
                )
            event["event_id"] = event_id
            event["schema"] = "taskplanner.observable_tool_event.v1"
            event["case_id"] = manifest["case_id"]
            event["time_sec"] = round(float(event["time_sec"]), 9)
            event["from"] = (
                None if event["event_type"] == "initial_state" else event.get("from")
            )
            event["derived_action"] = derive_action(event)
            event["review_status"] = status
            event["review"] = {
                "reviewer_kind": "human",
                "reviewer_id": reviewer_id,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
                "notes": str(payload.get("review_notes", "")).strip(),
            }
            if status in {"confirmed", "ambiguous"}:
                event["label_origin"] = "human_video_review"

            # A reviewed candidate leaves the proposal queue but keeps provenance
            # inside the reviewed record.
            events = [item for item in events if item["event_id"] != event_id]
            candidates = [
                item for item in candidates if item["event_id"] != event_id
            ]
            events.append(event)
            events.sort(key=lambda item: (float(item["time_sec"]), item["event_id"]))
            candidates.sort(
                key=lambda item: (float(item["time_sec"]), item["event_id"])
            )

            duration = float(manifest["duration_sec"])
            errors = validate_records(
                events,
                schema=self.schema,
                tool_catalog=self.tool_catalog,
                case_id=manifest["case_id"],
                duration_sec=duration,
            )
            errors.extend(
                f"candidate {message}"
                for message in validate_records(
                    candidates,
                    schema=self.schema,
                    tool_catalog=self.tool_catalog,
                    case_id=manifest["case_id"],
                    duration_sec=duration,
                )
            )
            if errors:
                raise InputError("\n".join(errors[:12]))

            counts = Counter(
                item["review_status"] for item in events + candidates
            )
            manifest["review_status_counts"] = {
                key: counts[key]
                for key in ("proposed", "confirmed", "ambiguous", "rejected")
            }
            confirmed = [
                item
                for item in events
                if item["review_status"] == "confirmed"
            ]
            human_confirmed_count = sum(
                item.get("label_origin") == "human_video_review"
                and item.get("review", {}).get("reviewer_kind") == "human"
                for item in confirmed
            )
            manifest["human_annotation"][
                "confirmed_event_count"
            ] = human_confirmed_count
            manifest["human_annotation"]["complete"] = False
            if "annotation_adjudication" in manifest:
                manifest["annotation_adjudication"].update(
                    {
                        "complete": False,
                        "confirmed_event_count": len(confirmed),
                        "confirmed_origin_counts": dict(
                            Counter(
                                item["label_origin"] for item in confirmed
                            )
                        ),
                        "confirmed_reviewer_kind_counts": dict(
                            Counter(
                                item["review"]["reviewer_kind"]
                                for item in confirmed
                                if item.get("review")
                            )
                        ),
                    }
                )

            event_path = self.case_dir / manifest["event_file"]
            candidate_path = self.case_dir / manifest["candidate_file"]
            self._write_jsonl(event_path, events)
            self._write_jsonl(candidate_path, candidates)
            manifest_tmp = self.manifest_path.with_name(
                self.manifest_path.name + ".tmp"
            )
            manifest_tmp.write_text(
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            manifest_tmp.replace(self.manifest_path)
            return {
                "ok": True,
                "event_id": event_id,
                "review_status": status,
                "state": self.state(),
            }


class InputError(Exception):
    pass


class ConflictError(Exception):
    pass


class FrameSource:
    def __init__(self, bag_dir: Path) -> None:
        self.bag_dir = bag_dir
        self.lock = threading.Lock()

    @lru_cache(maxsize=64)
    def frame(self, view: str, timestamp_ns: int) -> tuple[bytes, int]:
        if view not in VIEW_TOPICS:
            raise InputError(f"지원하지 않는 view: {view}")
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        from sensor_msgs.msg import CompressedImage

        # The ROS bag reader is not guaranteed to be thread-safe.  Requests can
        # run concurrently, so each request gets a short-lived filtered reader.
        with self.lock:
            reader = rosbag2_py.SequentialReader()
            reader.open(
                rosbag2_py.StorageOptions(
                    uri=str(self.bag_dir),
                    storage_id="mcap",
                ),
                rosbag2_py.ConverterOptions("cdr", "cdr"),
            )
            reader.set_filter(
                rosbag2_py.StorageFilter(topics=[VIEW_TOPICS[view]])
            )
            reader.seek(timestamp_ns)
            if not reader.has_next():
                reader.seek(0)
            while reader.has_next():
                topic, payload, actual_ns = read_next_record(reader)
                if topic != VIEW_TOPICS[view]:
                    continue
                message = deserialize_message(payload, CompressedImage)
                close_reader(reader)
                return bytes(message.data), int(actual_ns)
            close_reader(reader)
        raise InputError(f"{view} 프레임을 찾지 못했습니다.")


def make_handler(
    store: AnnotationStore,
    frames: FrameSource,
    static_dir: Path,
):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ObservableAnnotationGUI/1.0"

        def log_message(self, format: str, *args: object) -> None:
            print(f"[annotation-gui] {self.address_string()} {format % args}")

        def send_json(
            self,
            value: Any,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/health":
                self.send_json(
                    {
                        "ok": True,
                        "case_id": store.case_dir.name,
                        "source_bag": str(store.source_bag_dir),
                    }
                )
                return
            if parsed.path == "/api/state":
                self.send_json(store.state())
                return
            if parsed.path == "/api/frame":
                try:
                    query = parse_qs(parsed.query)
                    view = query.get("view", [""])[0]
                    time_sec = max(
                        0.0,
                        min(
                            float(query.get("time_sec", ["0"])[0]),
                            float(store.state()["duration_sec"]),
                        ),
                    )
                    data, actual_ns = frames.frame(
                        view,
                        round(time_sec * 1_000_000_000),
                    )
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("X-Bag-Timestamp-Ns", str(actual_ns))
                    self.send_header("Cache-Control", "private, max-age=60")
                    self.end_headers()
                    self.wfile.write(data)
                except (InputError, ValueError) as exc:
                    self.send_json(
                        {"ok": False, "error": str(exc)},
                        HTTPStatus.BAD_REQUEST,
                    )
                return

            requested = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
            target = (static_dir / requested).resolve()
            if static_dir.resolve() not in target.parents and target != static_dir.resolve():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = target.read_bytes()
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/review":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    raise InputError("요청 본문 크기가 올바르지 않습니다.")
                payload = json.loads(self.rfile.read(length))
                self.send_json(store.save_review(payload))
            except ConflictError as exc:
                self.send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.CONFLICT,
                )
            except (InputError, KeyError, TypeError, ValueError) as exc:
                self.send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.BAD_REQUEST,
                )

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local CAM4/FLIR observable event annotation GUI."
    )
    parser.add_argument("--case-dir", type=Path, required=True)
    parser.add_argument("--source-bag", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()

    static_dir = Path(__file__).with_name("web")
    store = AnnotationStore(
        case_dir=args.case_dir.resolve(),
        schema_path=args.schema.resolve(),
        tools_path=args.tools.resolve(),
        source_bag_dir=args.source_bag.resolve(),
    )
    manifest = store.state()
    expected_source = Path(
        json.loads(store.manifest_path.read_text(encoding="utf-8"))["source_bag"][
            "directory"
        ]
    ).resolve()
    if args.source_bag.resolve() != expected_source:
        raise SystemExit("source bag does not match annotation manifest")

    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(store, FrameSource(args.source_bag.resolve()), static_dir),
    )
    print(
        f"Annotation GUI: http://{args.host}:{args.port}/ "
        f"(case={manifest['case_id']}, proposals={len(manifest['candidates'])})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
