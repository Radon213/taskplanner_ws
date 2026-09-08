"""Blank CompressedImage publisher for image-input-unavailable VLM testing."""

from __future__ import annotations

from io import BytesIO
import json
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont
from procedure_spec import get_default_spec_dir, load_bundle
from procedure_spec.scenario_consumer import (
    ScenarioConfigConsumerBinding,
    scenario_config_apply_is_safe,
)
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import (
    DurabilityPolicy,
    QoSProfile,
    ReliabilityPolicy,
    qos_profile_sensor_data,
)
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from surgical_msgs.msg import SimulationState, SkillStatus


RECOVERY_ACTIONS = {
    "pick_up_from_mayo_and_handover",
    "retrieve_from_mayo",
    "retrieve_from_hand",
    "tool_retrieve",
}
RECOVERY_STARTED_STATES = {
    "dispatching",
    "accepted",
    "executing",
    "retrieving_from_mayo",
    "inserting_into_cleaner",
    "cleaning",
    "returning_to_rack",
    "completed",
}
RECOVERY_FAILURE_STATES = {
    "rejected",
    "dispatch_failed",
    "server_unavailable",
    "skipped_while_busy",
    "cancel_requested",
    "canceled",
    "aborted",
    "result_failed",
}


class NoImageCameraNode(Node):
    def __init__(self) -> None:
        super().__init__("no_image_camera")
        self.declare_parameter("image_topic", "/surgery/images/field/compressed")
        self.declare_parameter("width", 1024)
        self.declare_parameter("height", 576)
        self.declare_parameter("fps", 30.0)
        self.declare_parameter("label", "")
        self.declare_parameter("jpeg_quality", 88)
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self.declare_parameter(
            "scenario_config_topic", "/simulation/scenario_config"
        )
        self.declare_parameter("actor_overlay_topic", "/surgeon/actor_overlay")
        self.declare_parameter("skill_status_topic", "/skill/status")

        self._image_topic = str(self.get_parameter("image_topic").value)
        self._width = int(self.get_parameter("width").value)
        self._height = int(self.get_parameter("height").value)
        self._fps = float(self.get_parameter("fps").value)
        self._label = str(self.get_parameter("label").value)
        self._jpeg_quality = int(self.get_parameter("jpeg_quality").value)
        self._spec_dir = str(self.get_parameter("spec_dir").value)
        # This renderer does not own scenario selection.  Keep its accepted
        # bundle root immutable for the process lifetime so a retained
        # ScenarioStore message cannot redirect it to arbitrary local files.
        self._scenario_config = ScenarioConfigConsumerBinding.from_spec_dir(
            self._spec_dir
        )
        self._scenario_config_topic = str(
            self.get_parameter("scenario_config_topic").value
        ).strip()
        if not self._scenario_config_topic:
            raise ValueError("scenario_config_topic must not be empty")
        self._scenario_state_received = False
        self._scenario_running = False
        self._scenario_execution_state = ""
        self._scenario_initial_idle = True
        self._mayo_tools: list[str] = []
        self._actor_mayo_tools: set[str] = set()
        self._mayo_removed_by_skill: set[str] = set()
        self._field_event_lines: list[str] = []
        self._tool_display_names = self._load_tool_display_names(self._spec_dir)
        self._speech = ""
        self._render_key: tuple[object, ...] | None = None
        self._publisher = self.create_publisher(
            CompressedImage,
            self._image_topic,
            qos_profile_sensor_data,
        )
        self._jpeg_payload = self._render_payload([])
        self.create_subscription(
            String,
            str(self.get_parameter("actor_overlay_topic").value),
            self._on_actor_overlay,
            20,
        )
        self.create_subscription(
            SkillStatus,
            str(self.get_parameter("skill_status_topic").value),
            self._on_skill_status,
            20,
        )
        self.create_subscription(
            SimulationState,
            "/simulation/state",
            self._on_simulation_state,
            20,
        )
        self.create_subscription(String, "/simulation/control_state", self._on_control, 20)
        self.create_subscription(
            String,
            self._scenario_config_topic,
            self._on_scenario_config,
            QoSProfile(
                depth=1,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self._timer = self.create_timer(self._period_sec(), self._publish)
        self.add_on_set_parameters_callback(self._on_parameters_changed)

    def _period_sec(self) -> float:
        return 1.0 / max(self._fps, 1.0)

    def _on_parameters_changed(self, params):
        if any(parameter.name == "scenario_config_topic" for parameter in params):
            return SetParametersResult(
                successful=False,
                reason=(
                    "scenario_config_topic is process-lifetime; restart this "
                    "perception node to rebind it"
                ),
            )
        rebuild_image = False
        rebuild_timer = False
        for parameter in params:
            if parameter.name == "width":
                self._width = int(parameter.value)
                rebuild_image = True
            elif parameter.name == "height":
                self._height = int(parameter.value)
                rebuild_image = True
            elif parameter.name == "label":
                self._label = str(parameter.value)
                rebuild_image = True
            elif parameter.name == "jpeg_quality":
                self._jpeg_quality = int(parameter.value)
                rebuild_image = True
            elif parameter.name == "spec_dir":
                self._spec_dir = str(parameter.value)
                self._tool_display_names = self._load_tool_display_names(self._spec_dir)
                binding = getattr(self, "_scenario_config", None)
                if binding is not None:
                    binding.note_local_spec_dir(self._spec_dir)
                self._mayo_tools = []
                self._actor_mayo_tools.clear()
                self._mayo_removed_by_skill.clear()
                self._field_event_lines = []
                self._speech = ""
                rebuild_image = True
            elif parameter.name == "fps":
                self._fps = float(parameter.value)
                rebuild_timer = True

        if rebuild_image:
            self._render_key = None
        if rebuild_timer:
            self._timer.cancel()
            self._timer = self.create_timer(self._period_sec(), self._publish)
        return SetParametersResult(successful=True)

    def _on_scenario_config(self, message: String) -> None:
        """Stage the selected scenario; this image source never selects it."""

        try:
            if not self._scenario_config.stage(message.data):
                return
        except Exception as exc:
            self.get_logger().warning(
                f"no-image camera scenario config ignored: {exc}"
            )
            return
        self._apply_pending_scenario_config_if_safe()

    def _apply_pending_scenario_config_if_safe(self) -> None:
        """Apply one staged revision through the local overlay reload hook."""

        binding = getattr(self, "_scenario_config", None)
        if binding is None:
            return
        snapshot = binding.pending_snapshot()
        if snapshot is None or not scenario_config_apply_is_safe(
            state_received=bool(getattr(self, "_scenario_state_received", False)),
            scenario_running=bool(getattr(self, "_scenario_running", False)),
            execution_state=getattr(self, "_scenario_execution_state", ""),
            initial_idle=bool(getattr(self, "_scenario_initial_idle", False)),
        ):
            return
        try:
            resolved = binding.revalidate_pending()
            if resolved is None:
                return
            snapshot, bundle = resolved
        except Exception as exc:
            binding.discard(snapshot)
            self.get_logger().warning(
                f"no-image camera scenario config rejected before local swap: {exc}"
            )
            return
        result = self.set_parameters_atomically(
            [Parameter(name="spec_dir", value=bundle.spec_dir)]
        )
        if not bool(getattr(result, "successful", False)):
            self.get_logger().warning(
                "no-image camera scenario config local swap rejected: "
                f"{getattr(result, 'reason', '') or 'unknown reason'}"
            )
            return
        if not binding.commit(snapshot, bundle):
            return
        self.get_logger().info(
            "no-image camera scenario revision applied locally: "
            f"{snapshot.bundle_name}@{snapshot.revision}"
        )

    def _on_simulation_state(self, msg: SimulationState) -> None:
        self._scenario_state_received = True
        self._scenario_running = bool(getattr(msg, "running", False))
        self._scenario_execution_state = str(
            getattr(msg, "execution_state", "") or ""
        ).strip()
        self._apply_pending_scenario_config_if_safe()

    def _on_control(self, msg: String) -> None:
        command, _, _detail = str(msg.data or "").strip().partition(":")
        command = command.strip().casefold()
        if command in {"start", "start_actors", "start_runtime", "resume"}:
            self._scenario_initial_idle = False
        elif command in {"pause", "stop", "reset"}:
            self._scenario_initial_idle = True
        self._apply_pending_scenario_config_if_safe()

    def _load_tool_display_names(self, spec_dir: str) -> dict[str, str]:
        try:
            spec = load_bundle(spec_dir)
        except Exception as exc:  # pragma: no cover - runtime configuration issue
            self.get_logger().warn(f"failed to load tool display names from {spec_dir}: {exc}")
            return {}
        return {
            instrument.id: instrument.display_name
            for instrument in spec.bundle.instruments
            if instrument.id and instrument.display_name
        }

    def _on_actor_overlay(self, msg: String) -> None:
        try:
            payload = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        mayo = payload.get("mayo", [])
        if isinstance(mayo, list):
            next_mayo_tools = [str(item).strip() for item in mayo if str(item).strip()]
            next_mayo_set = set(next_mayo_tools)
            reintroduced_tools = next_mayo_set.difference(self._actor_mayo_tools)
            if reintroduced_tools:
                self._mayo_removed_by_skill.difference_update(reintroduced_tools)
            self._mayo_tools = next_mayo_tools
            self._actor_mayo_tools = next_mayo_set
            self._mayo_removed_by_skill.intersection_update(next_mayo_set)
        field_event = payload.get("field_event", [])
        if isinstance(field_event, list):
            self._field_event_lines = [str(item).strip() for item in field_event if str(item).strip()]
        elif isinstance(field_event, str) and field_event.strip():
            self._field_event_lines = [field_event.strip()]
        else:
            self._field_event_lines = []
        speech = str(payload.get("speech", "") or "")
        self._speech = speech[:80]
        self._render_key = None

    def _on_skill_status(self, msg: SkillStatus) -> None:
        action = (msg.action or "").strip()
        state = (msg.state or "").strip()
        tool_id = (msg.instrument_id or "").strip()
        if not tool_id:
            return
        if action not in RECOVERY_ACTIONS:
            return
        if state in RECOVERY_FAILURE_STATES:
            return
        if state not in RECOVERY_STARTED_STATES:
            return
        if state == "completed" and not bool(msg.success):
            return
        if tool_id not in self._mayo_removed_by_skill:
            self._mayo_removed_by_skill.add(tool_id)
            self._render_key = None

    def _visible_mayo_tools(self) -> list[str]:
        return [tool_id for tool_id in self._mayo_tools if tool_id not in self._mayo_removed_by_skill]

    def _display_tool_name(self, tool_id: str) -> str:
        return self._tool_display_names.get(tool_id, tool_id)

    def _overlay_lines(self) -> list[str]:
        lines: list[str] = []
        lines.extend(self._field_event_lines)
        mayo_tools = self._visible_mayo_tools()
        if mayo_tools:
            lines.append("Mayo stand:")
            lines.extend(f"- {self._display_tool_name(tool_id)}" for tool_id in mayo_tools)
        return lines

    def _render_payload(self, overlay_lines: list[str]) -> bytes:
        image = Image.new("RGB", (self._width, self._height), "#000000")
        draw = ImageDraw.Draw(image)
        font = self._font()
        label = self._label.strip()
        text_width = 0
        text_height = 0
        if label:
            text_bbox = draw.textbbox((0, 0), label, font=font)
            text_width = text_bbox[2] - text_bbox[0]
            text_height = text_bbox[3] - text_bbox[1]
        x = (self._width - text_width) // 2
        overlay_font, overlay_lines = self._fit_overlay(draw, overlay_lines)
        line_gap = max(12, self._height // 44)
        overlay_height = 0
        overlay_bboxes = []
        for line in overlay_lines:
            bbox = draw.textbbox((0, 0), line, font=overlay_font)
            overlay_bboxes.append(bbox)
            overlay_height += (bbox[3] - bbox[1]) + line_gap
        total_height = text_height
        if label and overlay_lines:
            total_height += line_gap * 2
        if overlay_lines:
            total_height += overlay_height
        y = max(0, (self._height - total_height) // 2)
        if label:
            draw.text((x, y), label, fill="#ffffff", font=font)
            y += text_height + line_gap * 2
        for line, bbox in zip(overlay_lines, overlay_bboxes, strict=False):
            line_width = bbox[2] - bbox[0]
            line_height = bbox[3] - bbox[1]
            line_x = (self._width - line_width) // 2
            draw.text((line_x, y), line, fill="#ffffff", font=overlay_font)
            y += line_height + line_gap
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=max(1, min(self._jpeg_quality, 100)))
        return buffer.getvalue()

    def _font_at(self, size: int):
        for path in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        return ImageFont.load_default()

    def _font(self):
        return self._font_at(max(24, self._width // 16))

    def _overlay_font(self, size: int | None = None):
        return self._font_at(size or max(42, self._width // 20))

    def _fit_overlay(self, draw: ImageDraw.ImageDraw, lines: list[str]):
        max_width = int(self._width * 0.9)
        max_height = int(self._height * 0.82)
        for size in range(max(42, self._width // 20), 25, -2):
            font = self._overlay_font(size)
            wrapped = self._wrap_lines(draw, lines, font, max_width)
            line_gap = max(12, self._height // 44)
            total_height = 0
            widest = 0
            for line in wrapped:
                bbox = draw.textbbox((0, 0), line, font=font)
                total_height += (bbox[3] - bbox[1]) + line_gap
                widest = max(widest, bbox[2] - bbox[0])
            if widest <= max_width and total_height <= max_height:
                return font, wrapped
        font = self._overlay_font(26)
        return font, self._wrap_lines(draw, lines, font, max_width)

    def _wrap_lines(
        self,
        draw: ImageDraw.ImageDraw,
        lines: Iterable[str],
        font,
        max_width: int,
    ) -> list[str]:
        wrapped: list[str] = []
        for line in lines:
            if not line:
                continue
            prefix = "- " if line.startswith("- ") else ""
            body = line[2:] if prefix else line
            words = body.split()
            if not words:
                wrapped.append(line)
                continue
            current = prefix + words[0]
            continuation_prefix = "  " if prefix else ""
            for word in words[1:]:
                candidate = f"{current} {word}"
                if self._text_width(draw, candidate, font) <= max_width:
                    current = candidate
                    continue
                wrapped.append(current)
                current = continuation_prefix + word
            wrapped.append(current)
        return wrapped

    def _text_width(self, draw: ImageDraw.ImageDraw, text: str, font) -> int:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]

    def _publish(self) -> None:
        overlay_lines = self._overlay_lines()
        render_key = (self._width, self._height, self._label, self._jpeg_quality, tuple(overlay_lines))
        if render_key != self._render_key:
            self._jpeg_payload = self._render_payload(overlay_lines)
            self._render_key = render_key
        msg = CompressedImage()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = "no_image_camera"
        msg.format = "jpeg"
        msg.data = self._jpeg_payload
        self._publisher.publish(msg)


def main() -> None:
    rclpy.init()
    node = NoImageCameraNode()
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
