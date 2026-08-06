"""Deterministic fault schedules used by release and replay campaigns."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
import hashlib
import json
import random
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageEnhance, ImageFilter
import yaml


SCHEMA = "taskplanner.fault_scenario.v1"
SUPPORTED_KINDS = {
    "drop",
    "freeze",
    "duplicate",
    "delay",
    "reorder",
    "corrupt",
    "blur",
    "exposure",
    "occlusion",
    "shake",
    "resize",
    "speech_replace",
    "speech_partial",
    "vlm_unhealthy",
    "vlm_invalid_schema",
    "vlm_timeout",
    "vlm_http_500",
    "vlm_restart",
}


@dataclass(frozen=True, slots=True)
class FaultEvent:
    event_id: str
    source: str
    kind: str
    start_sec: float
    duration_sec: float
    params: dict[str, Any] = field(default_factory=dict)

    def active(self, elapsed_sec: float) -> bool:
        return self.start_sec <= elapsed_sec < self.start_sec + self.duration_sec


@dataclass(frozen=True, slots=True)
class FaultScenario:
    scenario_id: str
    seed: int
    events: tuple[FaultEvent, ...]

    @classmethod
    def load(cls, path: str | Path) -> "FaultScenario":
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            raise ValueError(f"fault scenario must use schema {SCHEMA}")
        events: list[FaultEvent] = []
        for index, row in enumerate(payload.get("events", [])):
            if not isinstance(row, dict):
                raise ValueError(f"fault event {index} must be an object")
            kind = str(row.get("kind", "")).strip()
            if kind not in SUPPORTED_KINDS:
                raise ValueError(f"unsupported fault kind: {kind or '<empty>'}")
            source = str(row.get("source", "")).strip()
            if not source:
                raise ValueError(f"fault event {index} has no source")
            start_sec = float(row.get("start_sec", 0.0))
            duration_sec = float(row.get("duration_sec", 0.0))
            if start_sec < 0.0 or duration_sec <= 0.0:
                raise ValueError(
                    f"fault event {index} must have start_sec >= 0 and duration_sec > 0"
                )
            events.append(
                FaultEvent(
                    event_id=str(row.get("id", f"fault-{index + 1}")),
                    source=source,
                    kind=kind,
                    start_sec=start_sec,
                    duration_sec=duration_sec,
                    params=dict(row.get("params", {})),
                )
            )
        return cls(
            scenario_id=str(payload.get("scenario_id", Path(path).stem)),
            seed=int(payload.get("seed", 0)),
            events=tuple(sorted(events, key=lambda event: (event.start_sec, event.event_id))),
        )

    def active(self, source: str, elapsed_sec: float) -> tuple[FaultEvent, ...]:
        return self.active_any((source,), elapsed_sec)

    def active_any(
        self,
        sources: Iterable[str],
        elapsed_sec: float,
    ) -> tuple[FaultEvent, ...]:
        source_set = {str(source) for source in sources}
        return tuple(
            event
            for event in self.events
            if event.source in source_set | {"*"} and event.active(elapsed_sec)
        )

    def deterministic_random(self, source: str, sequence: int, event_id: str) -> random.Random:
        material = f"{self.seed}:{source}:{sequence}:{event_id}".encode("utf-8")
        digest = hashlib.sha256(material).digest()
        return random.Random(int.from_bytes(digest[:8], "big"))


def compact_fault_report(
    scenario: FaultScenario,
    counters: dict[str, dict[str, int]],
) -> str:
    return json.dumps(
        {
            "schema": "taskplanner.fault_report.v1",
            "scenario_id": scenario.scenario_id,
            "seed": scenario.seed,
            "counters": counters,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def transform_image_bytes(
    data: bytes,
    *,
    events: Iterable[FaultEvent],
    scenario: FaultScenario,
    source: str,
    sequence: int,
) -> bytes:
    events = tuple(events)
    if any(event.kind == "corrupt" for event in events):
        ratio = min(
            0.95,
            max(
                0.01,
                float(next(event for event in events if event.kind == "corrupt").params.get("keep_ratio", 0.25)),
            ),
        )
        return data[: max(1, int(len(data) * ratio))]
    visual = {
        "blur",
        "exposure",
        "occlusion",
        "shake",
        "resize",
    }
    if not any(event.kind in visual for event in events):
        return data

    image = Image.open(BytesIO(data)).convert("RGB")
    for event in events:
        params = event.params
        if event.kind == "blur":
            image = image.filter(
                ImageFilter.GaussianBlur(radius=max(0.0, float(params.get("radius", 4.0))))
            )
        elif event.kind == "exposure":
            image = ImageEnhance.Brightness(image).enhance(
                max(0.05, float(params.get("factor", 0.45)))
            )
        elif event.kind == "resize":
            width = max(16, int(params.get("width", image.width // 2)))
            height = max(16, int(params.get("height", image.height // 2)))
            image = image.resize((width, height), Image.Resampling.BILINEAR)
        elif event.kind == "shake":
            rng = scenario.deterministic_random(source, sequence, event.event_id)
            max_px = max(1, int(params.get("max_px", 18)))
            x = rng.randint(-max_px, max_px)
            y = rng.randint(-max_px, max_px)
            shifted = Image.new("RGB", image.size, (0, 0, 0))
            shifted.paste(image, (x, y))
            image = shifted
        elif event.kind == "occlusion":
            rng = scenario.deterministic_random(source, sequence, event.event_id)
            width_ratio = min(0.95, max(0.05, float(params.get("width_ratio", 0.35))))
            height_ratio = min(0.95, max(0.05, float(params.get("height_ratio", 0.35))))
            width = max(1, int(image.width * width_ratio))
            height = max(1, int(image.height * height_ratio))
            x = rng.randint(0, max(0, image.width - width))
            y = rng.randint(0, max(0, image.height - height))
            patch = Image.new("RGB", (width, height), (0, 0, 0))
            image.paste(patch, (x, y))

    output = BytesIO()
    image.save(output, format="JPEG", quality=85)
    return output.getvalue()


def transform_speech_text(text: str, events: Iterable[FaultEvent]) -> tuple[str, bool]:
    transformed = str(text)
    is_final = True
    for event in events:
        if event.kind == "speech_replace":
            replacements = event.params.get("replacements", {})
            if isinstance(replacements, dict):
                for source, target in replacements.items():
                    transformed = transformed.replace(str(source), str(target))
        elif event.kind == "speech_partial":
            words = transformed.split()
            keep_words = max(1, int(event.params.get("keep_words", max(1, len(words) // 2))))
            transformed = " ".join(words[:keep_words])
            is_final = False
    return transformed, is_final
