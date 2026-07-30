from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "create_replay_asset_map.py"
)
SPEC = importlib.util.spec_from_file_location(
    "create_replay_asset_map", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


def build_assets(root: Path) -> list[list[str]]:
    original = root / "original"
    shadow = root / "shadow"
    review = root / "review"
    rfdetr = root / "rfdetr"
    derived = root / "derived"

    for case_id in MODULE.CASE_IDS:
        for camera_number in range(1, 5):
            touch(original / case_id / f"cam_{camera_number}" / "rgb.avi")
        touch(original / case_id / "flir" / "rgb.avi")
        touch(shadow / "bags" / case_id / "metadata.yaml")
        touch(shadow / "bags" / case_id / f"{case_id}.mcap")

    for case_id in MODULE.REVIEW_CASE_IDS:
        for filename in (
            "review_cam2.mp4",
            "review_cam3.mp4",
            "review_cam4.mp4",
            "review_flir.mp4",
            "review_multiview.manifest.json",
        ):
            touch(review / case_id / filename)

    touch(rfdetr / "models" / "flir" / "checkpoint.pth")
    touch(derived / "0704_6" / "annotated.mcap")
    return [
        ["original_media", str(original), "audit"],
        ["shadow_dataset", str(shadow), "replay,evaluation"],
        ["review_media", str(review), "replay,evaluation"],
        ["rfdetr_assets", str(rfdetr), "live,replay"],
        ["derived_bags", str(derived), "evaluation"],
    ]


def build_release(root: Path) -> tuple[Path, Path]:
    release = root / "release"
    taskplanner = release / "source" / "taskplanner_ws"
    (release / "manifests").mkdir(parents=True)
    for case_id in MODULE.CASE_IDS:
        touch(
            taskplanner
            / "annotations"
            / "observable_tool_events"
            / "cases"
            / case_id
            / "annotation_manifest.json"
        )
    for case_id in MODULE.REVIEW_CASE_IDS:
        touch(
            taskplanner
            / "annotations"
            / "clinical_video"
            / "cases"
            / case_id
            / "clinical_manifest.v2.json"
        )
    touch(taskplanner / "reports" / "marker.json")
    return release, taskplanner


def run_main(
    monkeypatch,
    release: Path,
    taskplanner: Path,
    assets: list[list[str]],
    *,
    annotations: Path | None = None,
) -> int:
    values = {name: path for name, path, _ in assets}
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(MODULE_PATH),
            str(release),
            "--taskplanner-commit",
            "deadbeef",
            "--original-media",
            values["original_media"],
            "--shadow-dataset",
            values["shadow_dataset"],
            "--review-media",
            values["review_media"],
            "--rfdetr",
            values["rfdetr_assets"],
            "--annotations",
            str(annotations or taskplanner / "annotations"),
            "--reports",
            str(taskplanner / "reports"),
            "--derived-bags",
            values["derived_bags"],
        ],
    )
    return MODULE.main()


def test_create_asset_map_references_assets_without_copying(
    tmp_path: Path, monkeypatch
) -> None:
    release, taskplanner = build_release(tmp_path)
    assets = build_assets(tmp_path / "assets")

    result = run_main(monkeypatch, release, taskplanner, assets)

    assert result == 0
    payload = json.loads(
        (release / "data" / "DATA_PACKAGE.json").read_text()
    )
    assert payload["storage_mode"] == "referenced"
    assert len(payload["assets"]) == 7
    assert all(
        item["storage_mode"] in {"referenced", "bundled_source"}
        for item in payload["assets"]
    )
    assert not (release / "data" / "original_media").exists()


def test_create_asset_map_reports_missing_required_file(
    tmp_path: Path, monkeypatch
) -> None:
    release, taskplanner = build_release(tmp_path)
    assets = build_assets(tmp_path / "assets")
    missing = (
        tmp_path
        / "assets"
        / "original"
        / "0704_17"
        / "flir"
        / "rgb.avi"
    )
    missing.unlink()

    result = run_main(monkeypatch, release, taskplanner, assets)

    assert result == 1
    assert not (release / "data").exists()


def test_external_annotations_are_recorded_as_referenced(
    tmp_path: Path, monkeypatch
) -> None:
    release, taskplanner = build_release(tmp_path)
    assets = build_assets(tmp_path / "assets")
    external_annotations = tmp_path / "external-annotations"
    (taskplanner / "annotations").rename(external_annotations)

    result = run_main(
        monkeypatch,
        release,
        taskplanner,
        assets,
        annotations=external_annotations,
    )

    assert result == 0
    payload = json.loads(
        (release / "data" / "DATA_PACKAGE.json").read_text()
    )
    annotations = next(
        item for item in payload["assets"] if item["name"] == "annotations"
    )
    assert annotations["storage_mode"] == "referenced"
