"""Environment-only worker configuration with fail-closed defaults."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

from . import UPSTREAM_COMMIT


class ConfigError(ValueError):
    """Raised for an unsafe or ambiguous worker configuration."""


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive")
    return value


def _float_01(name: str, default: float) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not 0.0 <= value <= 1.0:
        raise ConfigError(f"{name} must be in [0, 1]")
    return value


def _finite_float(name: str, default: float) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not float("-inf") < value < float("inf"):
        raise ConfigError(f"{name} must be finite")
    return value


def _unit_vector_csv(
    name: str, default: tuple[float, float, float]
) -> tuple[float, float, float]:
    raw = os.environ.get(name, ",".join(str(item) for item in default))
    parts = [item.strip() for item in raw.split(",")]
    if len(parts) != 3:
        raise ConfigError(f"{name} must contain exactly three comma-separated numbers")
    try:
        values = tuple(float(item) for item in parts)
    except ValueError as exc:
        raise ConfigError(f"{name} must contain exactly three numbers") from exc
    if any(not float("-inf") < item < float("inf") for item in values):
        raise ConfigError(f"{name} must contain finite numbers")
    length_squared = sum(item * item for item in values)
    if length_squared <= 1.0e-18:
        raise ConfigError(f"{name} must not be a zero vector")
    return values  # SupportPlane normalizes it at the pinned algorithm boundary.


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "1" if default else "0").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean")


@dataclass(frozen=True)
class WorkerConfig:
    upstream_root: Path
    expected_upstream_commit: str
    model_root: Path
    tool_checkpoint: Path
    blood_checkpoint: Path
    hand_model: Path
    tool_ontology: Path
    host: str = "127.0.0.1"
    port: int = 8020
    api_token_file: Path | None = None
    device_policy: str = "cuda_required"
    optimize_rfdetr: bool = True
    tool_threshold: float = 0.3
    blood_threshold: float = 0.5
    max_hands: int = 2
    max_request_bytes: int = 20 * 1024 * 1024
    max_metadata_bytes: int = 64 * 1024
    max_rgb_bytes: int = 8 * 1024 * 1024
    max_decoded_rgb_bytes: int = 16 * 1024 * 1024
    max_depth_bytes: int = 10 * 1024 * 1024
    max_image_pixels: int = 4_194_304
    max_detections_per_algorithm: int = 100
    max_total_rle_counts: int = 1_000_000
    max_response_json_bytes: int = 16 * 1024 * 1024
    max_ingress_read_sec: float = 1.0
    max_deadline_ahead_ms: int = 15_000
    max_rgb_depth_skew_ns: int = 50_000_000
    depth_min_m: float = 0.05
    depth_max_m: float = 10.0
    tool_support_plane_normal: tuple[float, float, float] = (
        0.04967945869867935,
        0.06010054902002815,
        -0.9969553026043332,
    )
    tool_support_plane_offset_m: float = 0.7951867203215164
    tool_support_plane_config_version: str = (
        "reference_mcap_first_frame_blue_plane_provisional"
    )
    tool_support_plane_inlier_ratio: float = 0.7407333333333334
    tool_support_plane_residual_p95_m: float = 0.00548064783090434
    tool_support_plane_validated: bool = False
    tool_support_plane_artifact: Path | None = None
    tool_support_plane_artifact_sha256: str = ""
    tool_support_plane_camera_serial: str = ""
    tool_support_plane_camera_profile: str = ""
    tool_support_plane_firmware_version: str = ""
    tool_support_plane_max_age_days: int = 30

    @classmethod
    def from_env(cls) -> WorkerConfig:
        root = Path(os.environ.get("PNU_UPSTREAM_ROOT", "/opt/hand-blood-tools"))
        model_root = Path(os.environ.get("PNU_MODEL_ROOT", "/models"))
        algorithm = root / "components/tool_runtime_v1_6/algorithm"
        token_raw = os.environ.get("PNU_API_TOKEN_FILE", "").strip()
        policy = os.environ.get("PNU_DEVICE_POLICY", "cuda_required").strip().lower()
        if policy not in {"cuda_required", "allow_cpu"}:
            raise ConfigError(
                "PNU_DEVICE_POLICY must be 'cuda_required' or 'allow_cpu'"
            )
        expected = (
            os.environ.get("PNU_EXPECTED_UPSTREAM_COMMIT", UPSTREAM_COMMIT)
            .strip()
            .lower()
        )
        if len(expected) != 40 or any(ch not in "0123456789abcdef" for ch in expected):
            raise ConfigError("PNU_EXPECTED_UPSTREAM_COMMIT must be a full SHA-1")
        depth_min_m = _finite_float("PNU_DEPTH_MIN_M", 0.05)
        depth_max_m = _finite_float("PNU_DEPTH_MAX_M", 10.0)
        if depth_min_m < 0.0 or depth_max_m <= depth_min_m:
            raise ConfigError(
                "PNU_DEPTH_MIN_M/PNU_DEPTH_MAX_M must define a non-negative increasing range"
            )
        support_plane_version = os.environ.get(
            "PNU_TOOL_SUPPORT_PLANE_CONFIG_VERSION",
            "reference_mcap_first_frame_blue_plane_provisional",
        ).strip()
        if (
            not support_plane_version
            or len(support_plane_version.encode("utf-8")) > 256
            or "\x00" in support_plane_version
        ):
            raise ConfigError(
                "PNU_TOOL_SUPPORT_PLANE_CONFIG_VERSION must be a bounded non-empty string"
            )
        max_total_rle_counts = _positive_int(
            "PNU_MAX_TOTAL_RLE_COUNTS", 1_000_000
        )
        if max_total_rle_counts > 1_000_000:
            raise ConfigError(
                "PNU_MAX_TOTAL_RLE_COUNTS must not exceed the reviewed 1000000-run bound"
            )
        max_response_json_bytes = _positive_int(
            "PNU_MAX_RESPONSE_JSON_BYTES", 16 * 1024 * 1024
        )
        if max_response_json_bytes > 16 * 1024 * 1024:
            raise ConfigError(
                "PNU_MAX_RESPONSE_JSON_BYTES must not exceed the bridge 16 MiB bound"
            )
        max_ingress_read_sec = _finite_float("PNU_MAX_INGRESS_READ_SEC", 1.0)
        if not 0.05 <= max_ingress_read_sec <= 10.0:
            raise ConfigError("PNU_MAX_INGRESS_READ_SEC must be in [0.05, 10.0]")
        support_plane_residual = _finite_float(
            "PNU_TOOL_SUPPORT_PLANE_RESIDUAL_P95_M", 0.00548064783090434
        )
        if support_plane_residual < 0.0:
            raise ConfigError(
                "PNU_TOOL_SUPPORT_PLANE_RESIDUAL_P95_M must be non-negative"
            )
        support_plane_artifact_raw = os.environ.get(
            "PNU_TOOL_SUPPORT_PLANE_ARTIFACT", ""
        ).strip()
        support_plane_artifact_digest = os.environ.get(
            "PNU_TOOL_SUPPORT_PLANE_ARTIFACT_SHA256", ""
        ).strip().lower()
        if support_plane_artifact_digest and (
            len(support_plane_artifact_digest) != 64
            or any(
                char not in "0123456789abcdef"
                for char in support_plane_artifact_digest
            )
        ):
            raise ConfigError(
                "PNU_TOOL_SUPPORT_PLANE_ARTIFACT_SHA256 must be a full SHA-256"
            )
        support_plane_camera_serial = os.environ.get(
            "PNU_TOOL_SUPPORT_PLANE_CAMERA_SERIAL", ""
        ).strip()
        if (
            len(support_plane_camera_serial.encode("utf-8")) > 80
            or "\x00" in support_plane_camera_serial
        ):
            raise ConfigError(
                "PNU_TOOL_SUPPORT_PLANE_CAMERA_SERIAL must be a bounded string"
            )
        support_plane_camera_profile = os.environ.get(
            "PNU_TOOL_SUPPORT_PLANE_CAMERA_PROFILE", ""
        ).strip()
        support_plane_firmware_version = os.environ.get(
            "PNU_TOOL_SUPPORT_PLANE_FIRMWARE_VERSION", ""
        ).strip()
        for name, value in (
            ("PNU_TOOL_SUPPORT_PLANE_CAMERA_PROFILE", support_plane_camera_profile),
            (
                "PNU_TOOL_SUPPORT_PLANE_FIRMWARE_VERSION",
                support_plane_firmware_version,
            ),
        ):
            if len(value.encode("utf-8")) > 120 or "\x00" in value:
                raise ConfigError(f"{name} must be a bounded string")
        return cls(
            upstream_root=root,
            expected_upstream_commit=expected,
            model_root=model_root,
            tool_checkpoint=Path(
                os.environ.get("PNU_TOOL_CHECKPOINT", str(model_root / "tool.pth"))
            ),
            blood_checkpoint=Path(
                os.environ.get("PNU_BLOOD_CHECKPOINT", str(model_root / "blood.pth"))
            ),
            hand_model=Path(
                os.environ.get(
                    "PNU_HAND_MODEL", str(model_root / "hand_landmarker.task")
                )
            ),
            tool_ontology=Path(
                os.environ.get(
                    "PNU_TOOL_ONTOLOGY", str(algorithm / "model/ontology.json")
                )
            ),
            host=os.environ.get("PNU_HOST", "127.0.0.1").strip(),
            port=_positive_int("PNU_PORT", 8020),
            api_token_file=Path(token_raw) if token_raw else None,
            device_policy=policy,
            optimize_rfdetr=_bool("PNU_OPTIMIZE_RFDETR", True),
            tool_threshold=_float_01("PNU_TOOL_THRESHOLD", 0.3),
            blood_threshold=_float_01("PNU_BLOOD_THRESHOLD", 0.5),
            max_hands=_positive_int("PNU_MAX_HANDS", 2),
            max_request_bytes=_positive_int("PNU_MAX_REQUEST_BYTES", 20 * 1024 * 1024),
            max_metadata_bytes=_positive_int("PNU_MAX_METADATA_BYTES", 64 * 1024),
            max_rgb_bytes=_positive_int("PNU_MAX_RGB_BYTES", 8 * 1024 * 1024),
            max_decoded_rgb_bytes=_positive_int(
                "PNU_MAX_DECODED_RGB_BYTES", 16 * 1024 * 1024
            ),
            max_depth_bytes=_positive_int("PNU_MAX_DEPTH_BYTES", 10 * 1024 * 1024),
            max_image_pixels=_positive_int("PNU_MAX_IMAGE_PIXELS", 4_194_304),
            max_detections_per_algorithm=_positive_int(
                "PNU_MAX_DETECTIONS_PER_ALGORITHM", 100
            ),
            max_total_rle_counts=max_total_rle_counts,
            max_response_json_bytes=max_response_json_bytes,
            max_ingress_read_sec=max_ingress_read_sec,
            max_deadline_ahead_ms=_positive_int("PNU_MAX_DEADLINE_AHEAD_MS", 15_000),
            max_rgb_depth_skew_ns=_positive_int(
                "PNU_MAX_RGB_DEPTH_SKEW_NS", 50_000_000
            ),
            depth_min_m=depth_min_m,
            depth_max_m=depth_max_m,
            tool_support_plane_normal=_unit_vector_csv(
                "PNU_TOOL_SUPPORT_PLANE_NORMAL",
                (
                    0.04967945869867935,
                    0.06010054902002815,
                    -0.9969553026043332,
                ),
            ),
            tool_support_plane_offset_m=_finite_float(
                "PNU_TOOL_SUPPORT_PLANE_OFFSET_M", 0.7951867203215164
            ),
            tool_support_plane_config_version=support_plane_version,
            tool_support_plane_inlier_ratio=_float_01(
                "PNU_TOOL_SUPPORT_PLANE_INLIER_RATIO", 0.7407333333333334
            ),
            tool_support_plane_residual_p95_m=support_plane_residual,
            tool_support_plane_validated=_bool(
                "PNU_TOOL_SUPPORT_PLANE_VALIDATED", False
            ),
            tool_support_plane_artifact=(
                Path(support_plane_artifact_raw)
                if support_plane_artifact_raw
                else None
            ),
            tool_support_plane_artifact_sha256=support_plane_artifact_digest,
            tool_support_plane_camera_serial=support_plane_camera_serial,
            tool_support_plane_camera_profile=support_plane_camera_profile,
            tool_support_plane_firmware_version=(
                support_plane_firmware_version
            ),
            tool_support_plane_max_age_days=_positive_int(
                "PNU_TOOL_SUPPORT_PLANE_MAX_AGE_DAYS", 30
            ),
        )

    def read_api_token(self) -> str | None:
        if self.api_token_file is None:
            return None
        try:
            payload = self.api_token_file.read_bytes()
        except OSError as exc:
            raise ConfigError(
                f"cannot read PNU_API_TOKEN_FILE: {self.api_token_file}"
            ) from exc
        if not payload or len(payload) > 4096 or b"\x00" in payload:
            raise ConfigError("PNU_API_TOKEN_FILE must contain 1..4096 text bytes")
        try:
            token = payload.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ConfigError("PNU_API_TOKEN_FILE must be UTF-8") from exc
        if not token or any(ch.isspace() for ch in token):
            raise ConfigError(
                "API bearer token must be non-empty and contain no whitespace"
            )
        return token

    @staticmethod
    def validate_bind_auth(host: str, api_token: str | None) -> None:
        """Require bearer auth for every non-loopback network bind."""

        normalized = str(host).strip().strip("[]")
        loopback = normalized.casefold() == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(normalized).is_loopback
            except ValueError:
                loopback = False
        if not loopback and api_token is None:
            raise ConfigError(
                "PNU_API_TOKEN_FILE is required for wildcard or non-loopback bind"
            )
