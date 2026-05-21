"""Synthetic OR scene renderer used as a VLM development camera."""

from __future__ import annotations

from io import BytesIO
import json
from typing import Any

from PIL import Image, ImageDraw, ImageFont
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from surgical_msgs.msg import SimulationState

from .common import anchor_positions, instrument_anchor_id, tool_display_name


ENTITY_COLORS = {
    "instrument_rack": "#103846",
    "cleaner_station": "#234a7e",
    "mayo_stand": "#17475d",
    "humanoid": "#1a635f",
    "surgical_bed": "#30495d",
    "surgeon": "#68452b",
    "unknown_zone": "#4a2f45",
}


class SyntheticSceneCameraNode(Node):
    def __init__(self) -> None:
        super().__init__("synthetic_scene_camera")
        self.declare_parameter("image_topic", "/surgery/images/synthetic/compressed")
        self.declare_parameter("width", 1024)
        self.declare_parameter("height", 576)
        self.declare_parameter("render_mode", "vlm")
        self._image_topic = str(self.get_parameter("image_topic").value)
        self._width = int(self.get_parameter("width").value)
        self._height = int(self.get_parameter("height").value)
        self._render_mode = str(self.get_parameter("render_mode").value).strip().lower() or "vlm"
        self._publisher = self.create_publisher(CompressedImage, self._image_topic, 10)
        self._last_state: SimulationState | None = None
        self.add_on_set_parameters_callback(self._on_parameters_changed)
        self.create_subscription(SimulationState, "/simulation/state", self._on_state, 20)

    def _on_parameters_changed(self, params):
        for parameter in params:
            if parameter.name == "width":
                self._width = int(parameter.value)
            elif parameter.name == "height":
                self._height = int(parameter.value)
            elif parameter.name == "render_mode":
                self._render_mode = str(parameter.value).strip().lower() or "vlm"
        return SetParametersResult(successful=True)

    def _on_state(self, msg: SimulationState) -> None:
        self._last_state = msg
        self._publisher.publish(self._render(msg))

    def _render(self, state: SimulationState) -> CompressedImage:
        layout_bundle = self._load_layout(state.layout_json)
        anchors = anchor_positions(layout_bundle)
        entities = {entity["id"]: entity for entity in layout_bundle.get("entities", [])}

        image = Image.new("RGB", (self._width, self._height), "#071923")
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()

        for entity in layout_bundle.get("entities", []):
            self._draw_entity(draw, entity, font)

        if self._render_mode != "vlm":
            self._draw_title(draw, state, font)
        self._draw_instruments(draw, state, anchors, entities, font, debug=(self._render_mode != "vlm"))

        msg = CompressedImage()
        msg.header.stamp = state.stamp
        msg.format = "jpeg"
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=88)
        msg.data = buffer.getvalue()
        return msg

    def _draw_title(self, draw: ImageDraw.ImageDraw, state: SimulationState, font) -> None:
        draw.rounded_rectangle((36, 18, 388, 90), radius=18, fill="#0b2835", outline="#163b49", width=2)
        draw.text((58, 30), "Synthetic VLM Camera", fill="#d9f2f4", font=font)
        draw.text(
            (58, 52),
            f"procedure={state.procedure_id} phase={state.filtered_phase} state={state.execution_state}",
            fill="#8fc5cb",
            font=font,
        )

    def _draw_entity(self, draw: ImageDraw.ImageDraw, entity: dict[str, Any], font) -> None:
        x0, y0, x1, y1 = self._rect(entity)
        fill = ENTITY_COLORS.get(str(entity.get("type", "")), "#193746")
        draw.rounded_rectangle((x0, y0, x1, y1), radius=28, fill=fill, outline="#7fcfd0", width=3)
        if self._render_mode != "vlm":
            label = str(entity.get("label", entity.get("id", ""))).upper()
            draw.rounded_rectangle((x0 + 12, y0 + 12, x0 + 170, y0 + 52), radius=14, fill="#091a24")
            draw.text((x0 + 24, y0 + 24), label, fill="#edf7f8", font=font)

        if str(entity.get("type", "")) == "surgical_bed":
            draw.rounded_rectangle((x0 + 42, y0 + 34, x1 - 42, y1 - 34), radius=28, outline="#86b5d4", width=3)
        elif str(entity.get("type", "")) == "mayo_stand":
            draw.line((x0 + 26, y1 - 6, x0 + 50, y1 + 18), fill="#9ac9d0", width=3)
            draw.line((x1 - 26, y1 - 6, x1 - 50, y1 + 18), fill="#9ac9d0", width=3)
        elif str(entity.get("type", "")) == "cleaner_station":
            draw.ellipse((x0 + 18, y0 + 18, x1 - 18, y1 - 18), outline="#b0d4ff", width=4)
        elif str(entity.get("type", "")) == "instrument_rack":
            for slot_row in range(3):
                top = y0 + 70 + slot_row * 56
                draw.rounded_rectangle((x0 + 18, top, x1 - 18, top + 28), radius=10, outline="#80c7d1", width=2)

    def _draw_instruments(
        self,
        draw: ImageDraw.ImageDraw,
        state: SimulationState,
        anchors: dict[str, tuple[float, float]],
        entities: dict[str, dict[str, Any]],
        font,
        *,
        debug: bool,
    ) -> None:
        grouped: dict[str, list[Any]] = {}
        layout_bundle = {"anchors": [], "entities": list(entities.values())}
        for instrument in state.instrument_states:
            anchor_id = instrument_anchor_id(instrument, layout_bundle | {"anchors": [{"id": key} for key in anchors]})
            grouped.setdefault(anchor_id, []).append(instrument)

        for anchor_id, instruments in grouped.items():
            if anchor_id not in anchors:
                continue
            base_x, base_y = anchors[anchor_id]
            px, py = self._to_px(base_x, base_y)
            for index, instrument in enumerate(instruments):
                offset_x = (index % 2) * 108 - 54
                offset_y = (index // 2) * 34
                label = tool_display_name(instrument.instrument_id)
                badge = instrument.lifecycle_stage or instrument.location_type
                fill = "#5de2ba" if not instrument.contaminated else "#ff8c76"
                if debug:
                    width = max(150, min(260, 12 + len(label) * 7))
                    rect = (
                        px - width // 2 + offset_x,
                        py - 18 + offset_y,
                        px + width // 2 + offset_x,
                        py + 14 + offset_y,
                    )
                    draw.rounded_rectangle(rect, radius=15, fill=fill, outline="#0b1f28", width=2)
                    draw.text((rect[0] + 12, rect[1] + 8), label, fill="#072128", font=font)
                    draw.text((rect[0] + 12, rect[1] - 14), badge.replace("_", " "), fill="#9dc5cb", font=font)
                else:
                    cx = px + offset_x
                    cy = py + offset_y
                    draw.ellipse((cx - 16, cy - 16, cx + 16, cy + 16), fill=fill, outline="#0b1f28", width=2)

    def _load_layout(self, layout_json: str) -> dict[str, Any]:
        if not layout_json.strip():
            return {"entities": [], "anchors": []}
        try:
            payload = json.loads(layout_json)
        except json.JSONDecodeError:
            return {"entities": [], "anchors": []}
        if not isinstance(payload, dict):
            return {"entities": [], "anchors": []}
        return payload

    def _rect(self, entity: dict[str, Any]) -> tuple[int, int, int, int]:
        x = float(entity.get("x", 0.0))
        y = float(entity.get("y", 0.0))
        width = float(entity.get("width", 10.0))
        height = float(entity.get("height", 10.0))
        x0, y0 = self._to_px(x - width / 2.0, y - height / 2.0)
        x1, y1 = self._to_px(x + width / 2.0, y + height / 2.0)
        return x0, y0, x1, y1

    def _to_px(self, x: float, y: float) -> tuple[int, int]:
        return int(x / 100.0 * self._width), int(y / 100.0 * self._height)


def main() -> None:
    rclpy.init()
    node = SyntheticSceneCameraNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.destroy_node()
        finally:
            if rclpy.ok():
                rclpy.shutdown()
