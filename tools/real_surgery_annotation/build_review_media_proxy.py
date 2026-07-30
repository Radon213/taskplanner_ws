#!/usr/bin/env python3
"""Build a seekable, bag-time-aligned review proxy from surgical source media.

The source AVIs are constant-rate capture files, while the authoritative CAM4
timeline contains corrected bag timestamps and may contain capture gaps.  This
builder therefore renders one side-by-side review image per source frame and
feeds those images to FFmpeg through an ffconcat file whose per-frame durations
come directly from the exact timeline.  The resulting MP4 is VFR: frame PTS
match bag-relative time (to the MP4 microsecond time base), including gaps.

Audio is reconstructed from the source MCAP rather than from a convenience
export.  The audio metadata, message timestamps, chunk sizes, and declared
sample count are validated before the PCM stream is trimmed and muxed.

Only cache outputs are written.  Source media, the timeline, and the MCAP are
opened read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Sequence


BUILDER_VERSION = "1.0.0"
MANIFEST_SCHEMA = "taskplanner.review_media_proxy_manifest.v1"
TIMELINE_SCHEMA = "taskplanner.video_frame_timeline.v1"
DEFAULT_AUDIO_INFO_TOPIC = "/surgery/audio/info"
DEFAULT_AUDIO_PCM_TOPIC = "/surgery/audio/pcm_s16le"
HASH_CHUNK_BYTES = 8 * 1024 * 1024
PTS_TOLERANCE_SEC = 2.5e-6
VIDEO_TIME_BASE_HZ = 1_000_000


class BuildError(RuntimeError):
    """An input, dependency, or generated artifact failed validation."""


@dataclass(frozen=True)
class TimelineSpec:
    case_id: str
    topic: str
    source_bag: Path
    source_fps: float
    timestamps_sec: tuple[float, ...]
    frame_durations_sec: tuple[float, ...]
    gaps: tuple[dict[str, Any], ...]
    segments: tuple[dict[str, Any], ...]

    @property
    def frame_count(self) -> int:
        return len(self.timestamps_sec)

    @property
    def start_sec(self) -> float:
        return self.timestamps_sec[0]

    @property
    def visual_end_sec(self) -> float:
        return self.timestamps_sec[-1]


@dataclass(frozen=True)
class AudioExtraction:
    pcm_bytes: bytes
    sample_rate: int
    channels: int
    sample_count: int
    duration_sec: float
    start_sec: float
    chunk_samples: int
    chunk_duration_ns: int
    chunk_count: int
    trimmed_samples: int
    info: dict[str, Any]


@dataclass(frozen=True)
class BuildOptions:
    panel_width: int
    panel_height: int
    jpeg_quality: int
    video_encoder: str
    preset: str
    crf: int
    gop_frames: int
    audio_bitrate: str
    audio_info_topic: str
    audio_pcm_topic: str


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def input_fingerprint(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise BuildError(f"input file does not exist: {resolved}")
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "size_bytes": stat.st_size,
        "sha256": sha256_file(resolved),
    }


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BuildError(f"expected JSON object: {path}")
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BuildError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise BuildError(f"{label} must be finite")
    return result


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BuildError(f"{label} must be a positive integer")
    return value


def derive_frame_durations(
    timestamps_sec: Sequence[float],
    source_fps: float,
) -> tuple[float, ...]:
    """Return per-frame durations while preserving every authoritative PTS."""

    if not timestamps_sec:
        raise BuildError("timeline has no timestamps")
    if not math.isfinite(source_fps) or source_fps <= 0:
        raise BuildError("source_fps must be finite and positive")
    if len(timestamps_sec) == 1:
        return (1.0 / source_fps,)

    deltas = [
        right - left
        for left, right in zip(timestamps_sec, timestamps_sec[1:])
    ]
    if any(not math.isfinite(delta) or delta <= 0 for delta in deltas):
        raise BuildError("timeline timestamps must be strictly increasing")

    gap_threshold = 1.5 / source_fps
    normal_deltas = [delta for delta in deltas if delta <= gap_threshold]
    if not normal_deltas:
        last_duration = 1.0 / source_fps
    else:
        # A median resists a small number of timestamp outliers while following
        # corrected capture cadence rather than forcing nominal AVI FPS.
        last_duration = statistics.median(normal_deltas)
    return tuple(deltas + [last_duration])


def quantize_durations_for_time_base(
    timestamps_sec: Sequence[float],
    final_duration_sec: float,
    *,
    time_base_hz: int = VIDEO_TIME_BASE_HZ,
) -> tuple[float, ...]:
    """Quantize absolute PTS first so per-frame rounding cannot accumulate."""

    if not timestamps_sec:
        raise BuildError("timeline has no timestamps")
    if time_base_hz <= 0:
        raise BuildError("time_base_hz must be positive")
    ticks = [round(value * time_base_hz) for value in timestamps_sec]
    if any(right <= left for left, right in zip(ticks, ticks[1:])):
        raise BuildError("timeline frames collide at the selected time base")
    final_ticks = round(final_duration_sec * time_base_hz)
    if final_ticks <= 0:
        raise BuildError("final frame duration is below the selected time base")
    durations_ticks = [
        right - left for left, right in zip(ticks, ticks[1:])
    ] + [final_ticks]
    return tuple(value / time_base_hz for value in durations_ticks)


def derive_gaps_and_segments(
    timestamps_sec: Sequence[float],
    source_fps: float,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    gap_threshold = 1.5 / source_fps
    gaps: list[dict[str, Any]] = []
    for before_index, (before, after) in enumerate(
        zip(timestamps_sec, timestamps_sec[1:])
    ):
        delta = after - before
        if delta > gap_threshold:
            gaps.append(
                {
                    "before_frame_idx": before_index,
                    "after_frame_idx": before_index + 1,
                    "before_time_sec": before,
                    "after_time_sec": after,
                    "delta_sec": delta,
                }
            )

    segments: list[dict[str, Any]] = []
    start_index = 0
    for gap in gaps:
        end_index = int(gap["before_frame_idx"])
        segments.append(
            {
                "start_frame_idx": start_index,
                "end_frame_idx": end_index,
                "frame_count": end_index - start_index + 1,
                "start_sec": timestamps_sec[start_index],
                "end_sec": timestamps_sec[end_index],
            }
        )
        start_index = int(gap["after_frame_idx"])
    end_index = len(timestamps_sec) - 1
    segments.append(
        {
            "start_frame_idx": start_index,
            "end_frame_idx": end_index,
            "frame_count": end_index - start_index + 1,
            "start_sec": timestamps_sec[start_index],
            "end_sec": timestamps_sec[end_index],
        }
    )
    return tuple(gaps), tuple(segments)


def _validate_declared_gaps(
    declared: Any,
    derived: Sequence[dict[str, Any]],
) -> None:
    if declared is None:
        declared = []
    if not isinstance(declared, list):
        raise BuildError("timeline gaps must be an array")
    if len(declared) != len(derived):
        raise BuildError(
            "timeline declared gaps do not match gaps derived from timestamps"
        )
    for index, (given, expected) in enumerate(zip(declared, derived)):
        if not isinstance(given, dict):
            raise BuildError(f"timeline gaps[{index}] must be an object")
        for key in ("before_frame_idx", "after_frame_idx"):
            if given.get(key) != expected[key]:
                raise BuildError(f"timeline gaps[{index}].{key} is inconsistent")
        for key in ("before_time_sec", "after_time_sec"):
            actual = _finite_number(
                given.get(key),
                f"timeline gaps[{index}].{key}",
            )
            if abs(actual - float(expected[key])) > 5e-10:
                raise BuildError(f"timeline gaps[{index}].{key} is inconsistent")
        if "delta_sec" in given:
            actual_delta = _finite_number(
                given["delta_sec"],
                f"timeline gaps[{index}].delta_sec",
            )
            if abs(actual_delta - float(expected["delta_sec"])) > 5e-10:
                raise BuildError(
                    f"timeline gaps[{index}].delta_sec is inconsistent"
                )


def load_timeline(path: Path, source_bag_override: Path | None) -> TimelineSpec:
    value = load_json_object(path)
    if value.get("schema") != TIMELINE_SCHEMA:
        raise BuildError(f"unsupported timeline schema: {value.get('schema')!r}")
    case_id = value.get("case_id")
    topic = value.get("topic")
    if not isinstance(case_id, str) or not case_id:
        raise BuildError("timeline case_id is missing")
    if not isinstance(topic, str) or not topic.startswith("/"):
        raise BuildError("timeline topic is invalid")
    source_fps = _finite_number(value.get("source_fps"), "timeline source_fps")
    if source_fps <= 0:
        raise BuildError("timeline source_fps must be positive")
    raw_timestamps = value.get("timestamps_sec")
    if not isinstance(raw_timestamps, list) or not raw_timestamps:
        raise BuildError("timeline timestamps_sec must be a non-empty array")
    timestamps = tuple(
        _finite_number(item, f"timeline timestamps_sec[{index}]")
        for index, item in enumerate(raw_timestamps)
    )
    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        raise BuildError("timeline timestamps_sec must be strictly increasing")
    if value.get("frame_count") != len(timestamps):
        raise BuildError("timeline frame_count does not match timestamps_sec")
    if abs(
        _finite_number(value.get("start_sec"), "timeline start_sec")
        - timestamps[0]
    ) > 5e-10:
        raise BuildError("timeline start_sec does not match first timestamp")
    if abs(
        _finite_number(value.get("end_sec"), "timeline end_sec")
        - timestamps[-1]
    ) > 5e-10:
        raise BuildError("timeline end_sec does not match last timestamp")

    source_bag_value = source_bag_override or Path(
        str(value.get("source_bag", ""))
    )
    source_bag = source_bag_value.expanduser().resolve()
    if not source_bag.is_dir():
        raise BuildError(f"source bag directory does not exist: {source_bag}")

    gaps, segments = derive_gaps_and_segments(timestamps, source_fps)
    _validate_declared_gaps(value.get("gaps"), gaps)
    durations = derive_frame_durations(timestamps, source_fps)
    return TimelineSpec(
        case_id=case_id,
        topic=topic,
        source_bag=source_bag,
        source_fps=source_fps,
        timestamps_sec=timestamps,
        frame_durations_sec=durations,
        gaps=gaps,
        segments=segments,
    )


def locate_mcap(source_bag: Path) -> Path:
    paths = sorted(source_bag.glob("*.mcap"))
    if len(paths) != 1:
        raise BuildError(
            f"expected exactly one MCAP in {source_bag}, found {len(paths)}"
        )
    return paths[0].resolve()


def executable_and_version(name: str) -> dict[str, str]:
    executable = shutil.which(name)
    if executable is None:
        raise BuildError(f"required executable not found: {name}")
    try:
        completed = subprocess.run(
            [executable, "-version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BuildError(f"cannot query {name} version: {exc}") from exc
    first_line = completed.stdout.splitlines()[0] if completed.stdout else ""
    if not first_line:
        raise BuildError(f"{name} returned no version")
    return {"path": str(Path(executable).resolve()), "version": first_line}


def run_json(command: Sequence[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            list(command),
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise BuildError(
            f"command failed: {' '.join(command)}\n{stderr[-4000:]}"
        ) from exc
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BuildError(
            f"command did not return JSON: {' '.join(command)}"
        ) from exc
    if not isinstance(value, dict):
        raise BuildError("ffprobe JSON root must be an object")
    return value


def probe_input_video(ffprobe: str, path: Path) -> dict[str, Any]:
    value = run_json(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            (
                "stream=codec_name,width,height,pix_fmt,avg_frame_rate,"
                "r_frame_rate,nb_frames,nb_read_frames,duration"
            ),
            "-of",
            "json",
            str(path),
        ]
    )
    streams = value.get("streams")
    if not isinstance(streams, list) or len(streams) != 1:
        raise BuildError(f"expected one video stream in {path}")
    stream = streams[0]
    if not isinstance(stream, dict):
        raise BuildError(f"invalid ffprobe stream for {path}")
    return stream


def validate_source_videos(
    ffprobe: str,
    cam4_avi: Path,
    flir_avi: Path,
    frame_count: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for label, path in (("cam4", cam4_avi), ("flir", flir_avi)):
        stream = probe_input_video(ffprobe, path)
        count_text = stream.get("nb_read_frames") or stream.get("nb_frames")
        try:
            count = int(count_text)
        except (TypeError, ValueError) as exc:
            raise BuildError(f"cannot determine {label} frame count") from exc
        if count != frame_count:
            raise BuildError(
                f"{label} frame count {count} != timeline {frame_count}"
            )
        width = int(stream.get("width", 0))
        height = int(stream.get("height", 0))
        if width <= 0 or height <= 0:
            raise BuildError(f"{label} has invalid dimensions")
        result[label] = {
            "codec_name": stream.get("codec_name"),
            "width": width,
            "height": height,
            "frame_count": count,
            "duration_sec": float(stream.get("duration", 0.0)),
            "avg_frame_rate": stream.get("avg_frame_rate"),
        }
    return result


def _load_ros_dependencies() -> tuple[Any, Any, Any, Any, Any]:
    try:
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        from std_msgs.msg import String, UInt8MultiArray
    except ImportError as exc:
        raise BuildError(
            "ROS 2 Python packages are unavailable; source the installed ROS "
            "environment before running this builder"
        ) from exc
    return (
        rosbag2_py,
        deserialize_message,
        String,
        UInt8MultiArray,
        getattr(rosbag2_py, "StorageFilter"),
    )


def _read_next(reader: Any) -> tuple[str, bytes, int]:
    read_next = getattr(reader, "read_next_ext", None) or reader.read_next
    result = read_next()
    if len(result) < 3:
        raise BuildError(f"unexpected rosbag record shape: {len(result)}")
    return str(result[0]), result[1], int(result[2])


def validate_audio_chunks(
    *,
    info: dict[str, Any],
    audio_timestamps_ns: Sequence[int],
    chunk_sizes_bytes: Sequence[int],
) -> dict[str, int | float]:
    """Pure validation helper used before writing the reconstructed WAV."""

    if info.get("encoding") != "pcm_s16le":
        raise BuildError("audio info encoding must be pcm_s16le")
    sample_rate = _positive_int(info.get("sample_rate"), "audio sample_rate")
    channels = _positive_int(info.get("channels"), "audio channels")
    chunk_samples = _positive_int(
        info.get("chunk_samples"),
        "audio chunk_samples",
    )
    sample_count = _positive_int(info.get("samples"), "audio samples")
    expected_chunk_bytes = chunk_samples * channels * 2
    if not audio_timestamps_ns:
        raise BuildError("audio PCM topic has no messages")
    if len(audio_timestamps_ns) != len(chunk_sizes_bytes):
        raise BuildError("audio timestamps and chunks have different lengths")
    for index, size in enumerate(chunk_sizes_bytes):
        if size != expected_chunk_bytes:
            raise BuildError(
                f"audio chunk {index} has {size} bytes; "
                f"expected {expected_chunk_bytes}"
            )

    exact_period = Fraction(chunk_samples * 1_000_000_000, sample_rate)
    if exact_period.denominator != 1:
        raise BuildError(
            "audio chunk period is not representable as integer nanoseconds"
        )
    chunk_duration_ns = exact_period.numerator
    declared_duration_ms = _finite_number(
        info.get("chunk_duration_ms"),
        "audio chunk_duration_ms",
    )
    if abs(declared_duration_ms - chunk_duration_ns / 1_000_000) > 1e-6:
        raise BuildError("audio chunk_duration_ms is inconsistent")
    for index, (left, right) in enumerate(
        zip(audio_timestamps_ns, audio_timestamps_ns[1:])
    ):
        if right - left != chunk_duration_ns:
            raise BuildError(
                f"audio timestamp delta after chunk {index} is "
                f"{right - left} ns; expected {chunk_duration_ns} ns"
            )

    capacity_samples = len(audio_timestamps_ns) * chunk_samples
    minimum_samples = capacity_samples - chunk_samples + 1
    if not minimum_samples <= sample_count <= capacity_samples:
        raise BuildError(
            "audio info samples must use all full chunks and at most one "
            "trimmed final chunk"
        )
    declared_duration_sec = _finite_number(
        info.get("duration_sec"),
        "audio duration_sec",
    )
    exact_duration_sec = sample_count / sample_rate
    if abs(declared_duration_sec - exact_duration_sec) > 0.5 / sample_rate:
        raise BuildError("audio duration_sec is inconsistent with samples")
    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "chunk_samples": chunk_samples,
        "sample_count": sample_count,
        "chunk_duration_ns": chunk_duration_ns,
        "duration_sec": exact_duration_sec,
        "trimmed_samples": capacity_samples - sample_count,
    }


def extract_audio_from_mcap(
    *,
    source_bag: Path,
    timeline: TimelineSpec,
    audio_info_topic: str,
    audio_pcm_topic: str,
) -> AudioExtraction:
    (
        rosbag2_py,
        deserialize_message,
        String,
        UInt8MultiArray,
        StorageFilter,
    ) = _load_ros_dependencies()
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(
            uri=str(source_bag),
            storage_id="mcap",
        ),
        rosbag2_py.ConverterOptions("cdr", "cdr"),
    )
    topic_types = {
        item.name: item.type
        for item in reader.get_all_topics_and_types()
    }
    expected_types = {
        audio_info_topic: "std_msgs/msg/String",
        audio_pcm_topic: "std_msgs/msg/UInt8MultiArray",
    }
    for topic, expected_type in expected_types.items():
        if topic_types.get(topic) != expected_type:
            raise BuildError(
                f"{topic} type is {topic_types.get(topic)!r}; "
                f"expected {expected_type}"
            )
    if timeline.topic not in topic_types:
        raise BuildError(f"timeline topic is absent from source bag: {timeline.topic}")
    reader.set_filter(
        StorageFilter(
            topics=[timeline.topic, audio_info_topic, audio_pcm_topic]
        )
    )

    info_records: list[tuple[int, str]] = []
    audio_timestamps_ns: list[int] = []
    pcm_chunks: list[bytes] = []
    video_timestamps_ns: list[int] = []
    try:
        while reader.has_next():
            topic, payload, timestamp_ns = _read_next(reader)
            if topic == timeline.topic:
                video_timestamps_ns.append(timestamp_ns)
            elif topic == audio_info_topic:
                message = deserialize_message(payload, String)
                info_records.append((timestamp_ns, str(message.data)))
            elif topic == audio_pcm_topic:
                message = deserialize_message(payload, UInt8MultiArray)
                audio_timestamps_ns.append(timestamp_ns)
                pcm_chunks.append(bytes(message.data))
    finally:
        close = getattr(reader, "close", None)
        if close is not None:
            close()

    if len(video_timestamps_ns) != timeline.frame_count:
        raise BuildError(
            f"source bag timeline topic has {len(video_timestamps_ns)} frames; "
            f"expected {timeline.frame_count}"
        )
    video_origin_ns = video_timestamps_ns[0]
    for index, (actual_ns, expected_sec) in enumerate(
        zip(video_timestamps_ns, timeline.timestamps_sec)
    ):
        expected_ns = round(expected_sec * 1_000_000_000)
        actual_relative_ns = actual_ns - video_origin_ns
        if actual_relative_ns != expected_ns:
            raise BuildError(
                f"timeline mismatch at frame {index}: MCAP "
                f"{actual_relative_ns} ns != JSON {expected_ns} ns"
            )

    if len(info_records) != 1:
        raise BuildError(
            f"expected one {audio_info_topic} message, found {len(info_records)}"
        )
    try:
        info = json.loads(info_records[0][1])
    except json.JSONDecodeError as exc:
        raise BuildError("audio info message is not valid JSON") from exc
    if not isinstance(info, dict):
        raise BuildError("audio info message must contain a JSON object")
    validated = validate_audio_chunks(
        info=info,
        audio_timestamps_ns=audio_timestamps_ns,
        chunk_sizes_bytes=[len(item) for item in pcm_chunks],
    )
    sample_rate = int(validated["sample_rate"])
    channels = int(validated["channels"])
    sample_count = int(validated["sample_count"])
    byte_count = sample_count * channels * 2
    pcm_bytes = b"".join(pcm_chunks)[:byte_count]
    if len(pcm_bytes) != byte_count:
        raise BuildError("reconstructed PCM is shorter than audio info samples")
    audio_start_sec = (
        audio_timestamps_ns[0] - video_origin_ns
    ) / 1_000_000_000
    if abs(audio_start_sec) < 0.5 / sample_rate:
        audio_start_sec = 0.0
    return AudioExtraction(
        pcm_bytes=pcm_bytes,
        sample_rate=sample_rate,
        channels=channels,
        sample_count=sample_count,
        duration_sec=float(validated["duration_sec"]),
        start_sec=audio_start_sec,
        chunk_samples=int(validated["chunk_samples"]),
        chunk_duration_ns=int(validated["chunk_duration_ns"]),
        chunk_count=len(pcm_chunks),
        trimmed_samples=int(validated["trimmed_samples"]),
        info=info,
    )


def write_pcm_wav(path: Path, audio: AudioExtraction) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(audio.channels)
        stream.setsampwidth(2)
        stream.setframerate(audio.sample_rate)
        stream.writeframes(audio.pcm_bytes)


def _ffconcat_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def create_ffconcat(
    path: Path,
    frame_paths: Sequence[Path],
    frame_durations_sec: Sequence[float],
) -> None:
    if len(frame_paths) != len(frame_durations_sec) or not frame_paths:
        raise BuildError("frame paths and durations must be equally non-empty")
    lines = ["ffconcat version 1.0"]
    for frame_path, duration in zip(frame_paths, frame_durations_sec):
        if not math.isfinite(duration) or duration <= 0:
            raise BuildError("ffconcat frame duration must be positive")
        lines.append(f"file {_ffconcat_quote(frame_path.resolve())}")
        # A standalone JPEG otherwise defaults to a 1/25 stream time base,
        # which rounds a corrected timestamp such as 0.068113225 to 0.08.
        # One-megahertz input cadence gives concat a microsecond time base,
        # matching the MP4 video track time scale used below.
        lines.append("option framerate 1000000/1")
        lines.append(f"duration {duration:.9f}")
    # Do not repeat the last file.  A common ffconcat workaround repeats it so
    # its duration is observable, but then requires ``-frames:v`` to suppress
    # the duplicate.  FFmpeg treats that output limit as an early mux stop and
    # truncates the longer audio track.  Here audio defines container EOF, and
    # the authoritative visual endpoint is the last frame PTS itself.
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_checked(command: Sequence[str], stage: str) -> None:
    try:
        subprocess.run(
            list(command),
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise BuildError(
            f"{stage} failed: {' '.join(command)}\n{stderr[-8000:]}"
        ) from exc


def frame_extraction_command(
    *,
    ffmpeg: str,
    cam4_avi: Path,
    flir_avi: Path,
    frames_pattern: Path,
    timeline: TimelineSpec,
    options: BuildOptions,
) -> list[str]:
    panel = (
        f"scale={options.panel_width}:{options.panel_height}:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={options.panel_width}:{options.panel_height}:"
        "(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    )
    filter_graph = (
        f"[0:v]{panel}[cam4];"
        f"[1:v]{panel}[flir];"
        "[cam4][flir]hstack=inputs=2:shortest=1,format=yuvj420p[review]"
    )
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(cam4_avi),
        "-i",
        str(flir_avi),
        "-filter_complex",
        filter_graph,
        "-map",
        "[review]",
        "-an",
        "-frames:v",
        str(timeline.frame_count),
        "-fps_mode",
        "passthrough",
        "-q:v",
        str(options.jpeg_quality),
        "-start_number",
        "0",
        str(frames_pattern),
    ]


def proxy_encoding_command(
    *,
    ffmpeg: str,
    concat_path: Path,
    wav_path: Path,
    output_path: Path,
    timeline: TimelineSpec,
    audio: AudioExtraction,
    options: BuildOptions,
) -> list[str]:
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
    ]
    if timeline.start_sec:
        command.extend(["-itsoffset", f"{timeline.start_sec:.9f}"])
    command.extend(
        [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
        ]
    )
    if audio.start_sec >= 0:
        if audio.start_sec:
            command.extend(["-itsoffset", f"{audio.start_sec:.9f}"])
    else:
        command.extend(["-ss", f"{-audio.start_sec:.9f}"])
    command.extend(
        [
            "-i",
            str(wav_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-fps_mode:v",
            "vfr",
            "-vf",
            "scale=iw:ih:in_range=pc:out_range=tv,format=yuv420p",
            "-c:v",
            options.video_encoder,
            "-preset",
            options.preset,
            "-crf",
            str(options.crf),
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "tv",
            "-g",
            str(options.gop_frames),
            "-keyint_min",
            str(options.gop_frames),
            "-sc_threshold",
            "0",
            "-force_key_frames",
            "expr:gte(t,n_forced*1)",
            "-c:a",
            "aac",
            "-b:a",
            options.audio_bitrate,
            "-ar",
            str(audio.sample_rate),
            "-ac",
            str(audio.channels),
            "-video_track_timescale",
            str(VIDEO_TIME_BASE_HZ),
            "-movflags",
            "+faststart",
            "-map_metadata",
            "-1",
            str(output_path),
        ]
    )
    return command


def _command_template(
    command: Sequence[str],
    replacements: dict[str, str],
) -> list[str]:
    result: list[str] = []
    ordered = sorted(replacements.items(), key=lambda item: -len(item[0]))
    for argument in command:
        rendered = argument
        for actual, placeholder in ordered:
            rendered = rendered.replace(actual, placeholder)
        result.append(rendered)
    return result


def probe_output(
    *,
    ffprobe: str,
    proxy_path: Path,
    timeline: TimelineSpec,
    audio: AudioExtraction,
    options: BuildOptions,
) -> dict[str, Any]:
    stream_value = run_json(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            (
                "format=duration,size:stream=index,codec_type,codec_name,"
                "pix_fmt,width,height,duration,nb_frames,sample_rate,channels"
            ),
            "-of",
            "json",
            str(proxy_path),
        ]
    )
    streams = stream_value.get("streams")
    if not isinstance(streams, list):
        raise BuildError("generated proxy has no streams")
    videos = [
        item
        for item in streams
        if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    audios = [
        item
        for item in streams
        if isinstance(item, dict) and item.get("codec_type") == "audio"
    ]
    if len(videos) != 1 or len(audios) != 1:
        raise BuildError("generated proxy must contain one video and one audio stream")
    video = videos[0]
    audio_stream = audios[0]
    if video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p":
        raise BuildError(
            "generated video must be H.264 yuv420p; got "
            f"{video.get('codec_name')} {video.get('pix_fmt')}"
        )
    expected_width = options.panel_width * 2
    if (
        int(video.get("width", 0)) != expected_width
        or int(video.get("height", 0)) != options.panel_height
    ):
        raise BuildError("generated proxy dimensions are incorrect")
    if audio_stream.get("codec_name") != "aac":
        raise BuildError("generated audio must be AAC")
    if (
        int(audio_stream.get("sample_rate", 0)) != audio.sample_rate
        or int(audio_stream.get("channels", 0)) != audio.channels
    ):
        raise BuildError("generated audio layout is incorrect")

    frame_value = run_json(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "json",
            str(proxy_path),
        ]
    )
    raw_frames = frame_value.get("frames")
    if not isinstance(raw_frames, list):
        raise BuildError("cannot inspect generated video frame timestamps")
    actual_timestamps: list[float] = []
    for index, item in enumerate(raw_frames):
        if not isinstance(item, dict) or "best_effort_timestamp_time" not in item:
            raise BuildError(f"generated frame {index} has no timestamp")
        actual_timestamps.append(float(item["best_effort_timestamp_time"]))
    if len(actual_timestamps) != timeline.frame_count:
        raise BuildError(
            f"generated proxy has {len(actual_timestamps)} video frames; "
            f"expected {timeline.frame_count}"
        )
    maximum_pts_error = 0.0
    for index, (actual, expected) in enumerate(
        zip(actual_timestamps, timeline.timestamps_sec)
    ):
        error = abs(actual - expected)
        maximum_pts_error = max(maximum_pts_error, error)
        if error > PTS_TOLERANCE_SEC:
            raise BuildError(
                f"generated frame {index} PTS {actual:.9f} differs from "
                f"timeline {expected:.9f} by {error:.9f}s"
            )

    expected_audio_end = max(0.0, audio.start_sec) + audio.duration_sec
    format_info = stream_value.get("format")
    if not isinstance(format_info, dict):
        raise BuildError("generated proxy has no format metadata")
    container_duration = float(format_info.get("duration", 0.0))
    if container_duration + 0.05 < expected_audio_end:
        raise BuildError(
            f"generated proxy ends at {container_duration:.6f}s before "
            f"audio end {expected_audio_end:.6f}s"
        )
    return {
        "container_duration_sec": container_duration,
        "video": video,
        "audio": audio_stream,
        "frame_count": len(actual_timestamps),
        "first_frame_pts_sec": actual_timestamps[0],
        "last_frame_pts_sec": actual_timestamps[-1],
        "maximum_frame_pts_error_sec": maximum_pts_error,
    }


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(
                value,
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _emit(stage: str, **fields: Any) -> None:
    print(canonical_json({"stage": stage, **fields}), flush=True)


def cache_matches(
    *,
    output_path: Path,
    manifest_path: Path,
    cache_key_sha256: str,
) -> bool:
    if not output_path.is_file() or not manifest_path.is_file():
        return False
    try:
        manifest = load_json_object(manifest_path)
    except BuildError:
        return False
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("cache_key_sha256") != cache_key_sha256
    ):
        return False
    output = manifest.get("output")
    if not isinstance(output, dict):
        return False
    if output.get("path") != str(output_path.resolve()):
        return False
    expected_size = output.get("size_bytes")
    expected_hash = output.get("sha256")
    if (
        not isinstance(expected_size, int)
        or output_path.stat().st_size != expected_size
        or not isinstance(expected_hash, str)
    ):
        return False
    return sha256_file(output_path) == expected_hash


def _options_dict(options: BuildOptions) -> dict[str, Any]:
    return {
        "panel_width": options.panel_width,
        "panel_height": options.panel_height,
        "jpeg_quality": options.jpeg_quality,
        "video_encoder": options.video_encoder,
        "preset": options.preset,
        "crf": options.crf,
        "gop_frames": options.gop_frames,
        "audio_bitrate": options.audio_bitrate,
        "audio_info_topic": options.audio_info_topic,
        "audio_pcm_topic": options.audio_pcm_topic,
        "video_track_timescale": VIDEO_TIME_BASE_HZ,
        "movflags": ["faststart"],
        "layout": "cam4_left_flir_right",
    }


def build_proxy(args: argparse.Namespace) -> dict[str, Any]:
    cam4_avi = args.cam4_avi.expanduser().resolve()
    flir_avi = args.flir_avi.expanduser().resolve()
    timeline_path = args.timeline.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    manifest_path = (
        args.manifest.expanduser().resolve()
        if args.manifest is not None
        else output_path.with_suffix(output_path.suffix + ".manifest.json")
    )
    if output_path == manifest_path:
        raise BuildError("output and manifest paths must differ")
    for path in (cam4_avi, flir_avi, timeline_path):
        if not path.is_file():
            raise BuildError(f"input file does not exist: {path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if (output_path.exists() or manifest_path.exists()) and not args.reuse:
        raise BuildError(
            "output or manifest already exists; pass --reuse to reuse a valid "
            "cache or atomically rebuild a stale cache"
        )

    options = BuildOptions(
        panel_width=args.panel_width,
        panel_height=args.panel_height,
        jpeg_quality=args.jpeg_quality,
        video_encoder=args.video_encoder,
        preset=args.preset,
        crf=args.crf,
        gop_frames=args.gop_frames,
        audio_bitrate=args.audio_bitrate,
        audio_info_topic=args.audio_info_topic,
        audio_pcm_topic=args.audio_pcm_topic,
    )
    for label, value in (
        ("panel_width", options.panel_width),
        ("panel_height", options.panel_height),
    ):
        if value <= 0 or value % 2:
            raise BuildError(f"{label} must be a positive even integer")
    if options.gop_frames <= 0:
        raise BuildError("gop_frames must be a positive integer")
    if not 2 <= options.jpeg_quality <= 31:
        raise BuildError("jpeg_quality must be between 2 and 31")
    if not 0 <= options.crf <= 51:
        raise BuildError("crf must be between 0 and 51")

    timeline = load_timeline(timeline_path, args.source_bag)
    mcap_path = locate_mcap(timeline.source_bag)
    metadata_path = timeline.source_bag / "metadata.yaml"
    ffmpeg_info = executable_and_version(args.ffmpeg)
    ffprobe_info = executable_and_version(args.ffprobe)
    ffmpeg = ffmpeg_info["path"]
    ffprobe = ffprobe_info["path"]

    _emit("fingerprinting_inputs")
    inputs = {
        "cam4_avi": input_fingerprint(cam4_avi),
        "flir_avi": input_fingerprint(flir_avi),
        "timeline": input_fingerprint(timeline_path),
        "source_mcap": input_fingerprint(mcap_path),
    }
    if metadata_path.is_file():
        inputs["source_bag_metadata"] = input_fingerprint(metadata_path)
    tools = {
        "ffmpeg": ffmpeg_info,
        "ffprobe": ffprobe_info,
        "python": {
            "path": str(Path(sys.executable).resolve()),
            "version": platform.python_version(),
        },
        "ros_distro": os.environ.get("ROS_DISTRO", ""),
    }
    cache_basis = {
        "builder_version": BUILDER_VERSION,
        "inputs": inputs,
        "tools": tools,
        "options": _options_dict(options),
    }
    cache_key_sha256 = sha256_value(cache_basis)
    if args.reuse and cache_matches(
        output_path=output_path,
        manifest_path=manifest_path,
        cache_key_sha256=cache_key_sha256,
    ):
        _emit(
            "reused",
            output=str(output_path),
            manifest=str(manifest_path),
            cache_key_sha256=cache_key_sha256,
        )
        return load_json_object(manifest_path)

    _emit("validating_source_videos")
    source_video_probe = validate_source_videos(
        ffprobe,
        cam4_avi,
        flir_avi,
        timeline.frame_count,
    )
    _emit("extracting_audio")
    audio = extract_audio_from_mcap(
        source_bag=timeline.source_bag,
        timeline=timeline,
        audio_info_topic=options.audio_info_topic,
        audio_pcm_topic=options.audio_pcm_topic,
    )

    with tempfile.TemporaryDirectory(
        dir=output_path.parent,
        prefix=f".{output_path.stem}.build.",
    ) as temporary_name:
        work_dir = Path(temporary_name)
        frames_dir = work_dir / "frames"
        frames_dir.mkdir()
        frames_pattern = frames_dir / "%06d.jpg"
        wav_path = work_dir / "audio.wav"
        concat_path = work_dir / "frames.ffconcat"
        temporary_proxy = work_dir / output_path.name

        write_pcm_wav(wav_path, audio)
        extract_command = frame_extraction_command(
            ffmpeg=ffmpeg,
            cam4_avi=cam4_avi,
            flir_avi=flir_avi,
            frames_pattern=frames_pattern,
            timeline=timeline,
            options=options,
        )
        _emit("rendering_side_by_side_frames", frame_count=timeline.frame_count)
        _run_checked(extract_command, "side-by-side frame extraction")
        frame_paths = sorted(frames_dir.glob("*.jpg"))
        if len(frame_paths) != timeline.frame_count:
            raise BuildError(
                f"rendered {len(frame_paths)} frames; "
                f"expected {timeline.frame_count}"
            )
        concat_durations = quantize_durations_for_time_base(
            timeline.timestamps_sec,
            timeline.frame_durations_sec[-1],
        )
        create_ffconcat(
            concat_path,
            frame_paths,
            concat_durations,
        )
        encode_command = proxy_encoding_command(
            ffmpeg=ffmpeg,
            concat_path=concat_path,
            wav_path=wav_path,
            output_path=temporary_proxy,
            timeline=timeline,
            audio=audio,
            options=options,
        )
        _emit("encoding_proxy")
        _run_checked(encode_command, "proxy encoding")
        _emit("verifying_proxy")
        media_probe = probe_output(
            ffprobe=ffprobe,
            proxy_path=temporary_proxy,
            timeline=timeline,
            audio=audio,
            options=options,
        )
        with temporary_proxy.open("rb") as stream:
            os.fsync(stream.fileno())

        replacements = {
            str(work_dir): "{work_dir}",
            str(temporary_proxy): "{output}",
        }
        command_templates = {
            "frame_extraction": _command_template(
                extract_command,
                replacements,
            ),
            "proxy_encoding": _command_template(
                encode_command,
                replacements,
            ),
        }
        os.replace(temporary_proxy, output_path)

    output_hash = sha256_file(output_path)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "builder_version": BUILDER_VERSION,
        "case_id": timeline.case_id,
        "cache_key_sha256": cache_key_sha256,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": inputs,
        "tools": tools,
        "options": _options_dict(options),
        "commands": command_templates,
        "timeline": {
            "topic": timeline.topic,
            "frame_count": timeline.frame_count,
            "start_sec": timeline.start_sec,
            "visual_end_sec": timeline.visual_end_sec,
            "final_frame_duration_sec": timeline.frame_durations_sec[-1],
            "timestamp_quantization_time_base_hz": VIDEO_TIME_BASE_HZ,
            "gaps": list(timeline.gaps),
            "segments": list(timeline.segments),
            "maximum_pts_error_sec": media_probe[
                "maximum_frame_pts_error_sec"
            ],
        },
        "audio": {
            "info_topic": options.audio_info_topic,
            "pcm_topic": options.audio_pcm_topic,
            "sample_rate": audio.sample_rate,
            "channels": audio.channels,
            "sample_count": audio.sample_count,
            "duration_sec": audio.duration_sec,
            "start_sec": audio.start_sec,
            "chunk_samples": audio.chunk_samples,
            "chunk_duration_ns": audio.chunk_duration_ns,
            "chunk_count": audio.chunk_count,
            "trimmed_samples": audio.trimmed_samples,
            "source_info": audio.info,
        },
        "source_video_probe": source_video_probe,
        "output": {
            "path": str(output_path),
            "manifest_path": str(manifest_path),
            "size_bytes": output_path.stat().st_size,
            "sha256": output_hash,
            "media_probe": media_probe,
        },
    }
    atomic_write_json(manifest_path, manifest)
    _emit(
        "complete",
        output=str(output_path),
        manifest=str(manifest_path),
        sha256=output_hash,
        duration_sec=media_probe["container_duration_sec"],
    )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a side-by-side, bag-time VFR H.264/AAC MP4 for the "
            "surgical event timeline reviewer."
        )
    )
    parser.add_argument("--cam4-avi", type=Path, required=True)
    parser.add_argument("--flir-avi", type=Path, required=True)
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument(
        "--source-bag",
        type=Path,
        help="Override timeline.source_bag.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Defaults to OUTPUT.mp4.manifest.json.",
    )
    parser.add_argument(
        "--reuse",
        action="store_true",
        help=(
            "Reuse only when manifest inputs, tools, options, and output hash "
            "match; otherwise atomically rebuild the stale cache."
        ),
    )
    parser.add_argument("--panel-width", type=int, default=640)
    parser.add_argument("--panel-height", type=int, default=360)
    parser.add_argument("--jpeg-quality", type=int, default=3)
    parser.add_argument("--video-encoder", default="libx264")
    parser.add_argument("--preset", default="veryfast")
    parser.add_argument("--crf", type=int, default=20)
    parser.add_argument("--gop-frames", type=int, default=30)
    parser.add_argument("--audio-bitrate", default="128k")
    parser.add_argument(
        "--audio-info-topic",
        default=DEFAULT_AUDIO_INFO_TOPIC,
    )
    parser.add_argument(
        "--audio-pcm-topic",
        default=DEFAULT_AUDIO_PCM_TOPIC,
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        build_proxy(args)
    except (BuildError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
