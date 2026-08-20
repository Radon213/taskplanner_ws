from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from tools.prompt_optimization.gesture_recognition.visual_review_gui.server import (
    DECISION_SCHEMA,
    GestureReviewHTTPServer,
    InputError,
    ReviewCatalog,
)


def _catalog(tmp_path: Path) -> ReviewCatalog:
    original = tmp_path / "output" / "original.jpg"
    crop = tmp_path / "output" / "crop.jpg"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"original-image")
    crop.write_bytes(b"crop-image")
    index = tmp_path / "review_index.json"
    index.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "index": 1,
                        "sample_id": "0704_6-R0001-positive_event_midpoint-f0001",
                        "case_id": "0704_6",
                        "event_id": "0704_6-R0001",
                        "frame_idx": 1,
                        "time_sec": 0.1,
                        "partition": "calibration",
                        "failure_type": "FN",
                        "sample_kind": "positive_event_midpoint",
                        "actual_label": "open_receive",
                        "predicted_gesture": "not_open_receive",
                        "raw_model_text": '{"open_hand":false}',
                        "original_cam4_image": "output/original.jpg",
                        "vlm_input_image": "output/crop.jpg",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return ReviewCatalog(
        repository_root=tmp_path,
        review_index_path=index,
        decisions_path=tmp_path / "decisions" / "decisions.jsonl",
    )


def test_catalog_keeps_source_labels_read_only_and_appends_separate_decisions(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    before = catalog.session()
    assert before["sample_count"] == 1
    assert before["reviewed_count"] == 0
    assert before["policy"]["original_event_labels_mutable"] is False
    assert before["samples"][0]["existing_event_proxy_open_hand"] is True
    assert before["samples"][0]["vlm_predicted_open_hand"] is False

    first = catalog.append_decision(
        {
            "sample_id": before["samples"][0]["sample_id"],
            "decision": "open_hand",
            "note": "손바닥이 보인다",
        }
    )
    second = catalog.append_decision(
        {
            "sample_id": before["samples"][0]["sample_id"],
            "decision": "ambiguous",
            "note": "다시 보니 손가락 일부가 가려짐",
        }
    )

    lines = catalog.decisions_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert all(record["schema"] == DECISION_SCHEMA for record in records)
    assert records[1]["supersedes_decision_id"] == first["decision_id"]
    assert second["visual_open_hand"] is None
    after = catalog.session()
    assert after["reviewed_count"] == 1
    assert after["decisions"][before["samples"][0]["sample_id"]]["decision"] == "ambiguous"


def test_catalog_rejects_invalid_visual_decision(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    with pytest.raises(InputError, match="유효하지 않은 visual decision"):
        catalog.append_decision(
            {
                "sample_id": "0704_6-R0001-positive_event_midpoint-f0001",
                "decision": "overwrite_ground_truth",
            }
        )


def test_catalog_inherits_existing_visual_decisions_as_read_only_seed(tmp_path: Path) -> None:
    source_catalog = _catalog(tmp_path)
    sample_id = "0704_6-R0001-positive_event_midpoint-f0001"
    seeded = source_catalog.append_decision(
        {"sample_id": sample_id, "decision": "open_hand", "note": "earlier review"}
    )
    full_index = tmp_path / "full_review_index.json"
    payload = json.loads(source_catalog.review_index_path.read_text(encoding="utf-8"))
    payload["metadata"] = {"title": "전체 open-hand 시각 검토"}
    full_index.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    full_catalog = ReviewCatalog(
        repository_root=tmp_path,
        review_index_path=full_index,
        decisions_path=tmp_path / "full-decisions" / "decisions.jsonl",
        seed_decision_paths=(source_catalog.decisions_path,),
    )

    inherited = full_catalog.session()
    assert inherited["reviewed_count"] == 1
    assert inherited["unreviewed_count"] == 0
    assert inherited["metadata"]["title"] == "전체 open-hand 시각 검토"
    assert inherited["decisions"][sample_id]["origin"] == "seed"

    current = full_catalog.append_decision(
        {"sample_id": sample_id, "decision": "not_open_hand", "note": "full queue review"}
    )
    assert current["origin"] == "current"
    assert current["supersedes_decision_id"] == seeded["decision_id"]
    assert full_catalog.session()["decisions"][sample_id]["origin"] == "current"


def test_http_api_serves_only_registered_images_and_appends_decision(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    server = GestureReviewHTTPServer(("127.0.0.1", 0), catalog)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    port = server.server_address[1]
    sample_id = "0704_6-R0001-positive_event_midpoint-f0001"
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", "/api/session")
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200
        assert payload["sample_count"] == 1

        connection.request("GET", f"/api/asset/{sample_id}/original")
        image_response = connection.getresponse()
        assert image_response.status == 200
        assert image_response.read() == b"original-image"

        connection.request("GET", "/api/asset/unknown/original")
        missing_response = connection.getresponse()
        assert missing_response.status == 400
        missing_response.read()

        body = json.dumps(
            {"sample_id": sample_id, "decision": "not_open_hand", "note": ""}
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/decision",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        saved_response = connection.getresponse()
        saved = json.loads(saved_response.read())
        assert saved_response.status == 201
        assert saved["decision"]["visual_open_hand"] is False
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
