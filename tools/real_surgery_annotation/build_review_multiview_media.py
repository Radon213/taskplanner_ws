#!/usr/bin/env python3
"""Build four independent, bag-time-aligned surgical review streams.

CAM4 is the authoritative corrected timeline and the only stream that carries
the reconstructed source-bag audio.  CAM2, CAM3, FLIR, and CAM4 are encoded as
separate H.264 MP4 files whose video frame PTS are identical.  The reviewer can
therefore render four independent ``<video>`` elements without relying on a
pre-composited image.

Only cache outputs are written.  Source AVIs, the timeline, and the MCAP are
opened read-only.
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from tools.real_surgery_annotation import build_review_media_proxy as base
except ModuleNotFoundError:
    # Keep direct ``python tools/.../build_review_multiview_media.py`` usage
    # working in addition to ``python -m tools...``.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.real_surgery_annotation import build_review_media_proxy as base


BUILDER_VERSION = "1.0.0"
MANIFEST_SCHEMA = "taskplanner.review_multiview_proxy_manifest.v1"
VIEW_ORDER = ("cam4", "flir", "cam2", "cam3")
MASTER_VIEW = "cam4"
OUTPUT_FILENAMES = {
    "cam4": "review_cam4.mp4",
    "flir": "review_flir.mp4",
    "cam2": "review_cam2.mp4",
    "cam3": "review_cam3.mp4",
}
SOURCE_RELATIVE_PATHS = {
    "cam4": Path("cam_4/rgb.avi"),
    "flir": Path("flir/rgb.avi"),
    "cam2": Path("cam_2/rgb.avi"),
    "cam3": Path("cam_3/rgb.avi"),
}


def _resolve_source_videos(args: argparse.Namespace) -> dict[str, Path]:
    source_root = (
        args.source_root.expanduser().resolve()
        if args.source_root is not None
        else None
    )
    explicit = {
        "cam4": args.cam4_avi,
        "flir": args.flir_avi,
        "cam2": args.cam2_avi,
        "cam3": args.cam3_avi,
    }
    result: dict[str, Path] = {}
    for view in VIEW_ORDER:
        given = explicit[view]
        if given is not None:
            result[view] = given.expanduser().resolve()
        elif source_root is not None:
            result[view] = (source_root / SOURCE_RELATIVE_PATHS[view]).resolve()
        else:
            raise base.BuildError(
                f"{view} source is missing; pass --{view}-avi or --source-root"
            )
    return result


def _validate_options(options: base.BuildOptions) -> None:
    for label, value in (
        ("panel_width", options.panel_width),
        ("panel_height", options.panel_height),
    ):
        if value <= 0 or value % 2:
            raise base.BuildError(f"{label} must be a positive even integer")
    if options.gop_frames <= 0:
        raise base.BuildError("gop_frames must be a positive integer")
    if not 2 <= options.jpeg_quality <= 31:
        raise base.BuildError("jpeg_quality must be between 2 and 31")
    if not 0 <= options.crf <= 51:
        raise base.BuildError("crf must be between 0 and 51")


def _source_frame_count(stream: Mapping[str, Any], view: str) -> int:
    count_text = stream.get("nb_read_frames") or stream.get("nb_frames")
    try:
        return int(count_text)
    except (TypeError, ValueError) as exc:
        raise base.BuildError(f"cannot determine {view} frame count") from exc


def validate_source_videos(
    *,
    ffprobe: str,
    source_videos: Mapping[str, Path],
    frame_count: int,
) -> dict[str, Any]:
    """Validate all four independent sources against the CAM4 timeline."""

    if set(source_videos) != set(VIEW_ORDER):
        raise base.BuildError(
            f"source views must be exactly {', '.join(VIEW_ORDER)} in order"
        )
    result: dict[str, Any] = {}
    for view in VIEW_ORDER:
        path = source_videos[view]
        stream = base.probe_input_video(ffprobe, path)
        count = _source_frame_count(stream, view)
        if count != frame_count:
            raise base.BuildError(
                f"{view} frame count {count} != timeline {frame_count}"
            )
        width = int(stream.get("width", 0))
        height = int(stream.get("height", 0))
        if width <= 0 or height <= 0:
            raise base.BuildError(f"{view} has invalid dimensions")
        result[view] = {
            "codec_name": stream.get("codec_name"),
            "width": width,
            "height": height,
            "frame_count": count,
            "duration_sec": float(stream.get("duration", 0.0)),
            "avg_frame_rate": stream.get("avg_frame_rate"),
        }
    return result


def single_view_frame_extraction_command(
    *,
    ffmpeg: str,
    source_avi: Path,
    frames_pattern: Path,
    timeline: base.TimelineSpec,
    options: base.BuildOptions,
) -> list[str]:
    """Return an extraction command for one view; no composition is allowed."""

    video_filter = (
        f"scale={options.panel_width}:{options.panel_height}:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={options.panel_width}:{options.panel_height}:"
        "(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuvj420p"
    )
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source_avi),
        "-map",
        "0:v:0",
        "-vf",
        video_filter,
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


def independent_proxy_encoding_command(
    *,
    ffmpeg: str,
    concat_path: Path,
    wav_path: Path,
    output_path: Path,
    timeline: base.TimelineSpec,
    audio: base.AudioExtraction,
    options: base.BuildOptions,
    include_audio: bool,
) -> list[str]:
    """Encode one independently seekable VFR stream."""

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
    if include_audio:
        if audio.start_sec >= 0:
            if audio.start_sec:
                command.extend(["-itsoffset", f"{audio.start_sec:.9f}"])
        else:
            command.extend(["-ss", f"{-audio.start_sec:.9f}"])
        command.extend(["-i", str(wav_path)])

    command.extend(
        [
            "-map",
            "0:v:0",
        ]
    )
    if include_audio:
        command.extend(["-map", "1:a:0"])
    else:
        command.append("-an")
    command.extend(
        [
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
        ]
    )
    if include_audio:
        command.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                options.audio_bitrate,
                "-ar",
                str(audio.sample_rate),
                "-ac",
                str(audio.channels),
            ]
        )
    command.extend(
        [
            "-video_track_timescale",
            str(base.VIDEO_TIME_BASE_HZ),
            "-movflags",
            "+faststart",
            "-map_metadata",
            "-1",
            str(output_path),
        ]
    )
    return command


def probe_independent_output(
    *,
    ffprobe: str,
    proxy_path: Path,
    timeline: base.TimelineSpec,
    audio: base.AudioExtraction,
    options: base.BuildOptions,
    include_audio: bool,
) -> dict[str, Any]:
    """Fail closed unless a generated stream has the authoritative video PTS."""

    stream_value = base.run_json(
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
        raise base.BuildError("generated proxy has no streams")
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
    if len(videos) != 1:
        raise base.BuildError("generated proxy must contain one video stream")
    if len(audios) != (1 if include_audio else 0):
        expected = "one" if include_audio else "no"
        raise base.BuildError(
            f"generated proxy must contain {expected} audio stream"
        )
    video = videos[0]
    if video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p":
        raise base.BuildError(
            "generated video must be H.264 yuv420p; got "
            f"{video.get('codec_name')} {video.get('pix_fmt')}"
        )
    if (
        int(video.get("width", 0)) != options.panel_width
        or int(video.get("height", 0)) != options.panel_height
    ):
        raise base.BuildError("generated proxy dimensions are incorrect")
    if include_audio:
        audio_stream = audios[0]
        if audio_stream.get("codec_name") != "aac":
            raise base.BuildError("generated audio must be AAC")
        if (
            int(audio_stream.get("sample_rate", 0)) != audio.sample_rate
            or int(audio_stream.get("channels", 0)) != audio.channels
        ):
            raise base.BuildError("generated audio layout is incorrect")

    frame_value = base.run_json(
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
        raise base.BuildError("cannot inspect generated video frame timestamps")
    actual_timestamps: list[float] = []
    for index, item in enumerate(raw_frames):
        if (
            not isinstance(item, dict)
            or "best_effort_timestamp_time" not in item
        ):
            raise base.BuildError(f"generated frame {index} has no timestamp")
        actual_timestamps.append(float(item["best_effort_timestamp_time"]))
    if len(actual_timestamps) != timeline.frame_count:
        raise base.BuildError(
            f"generated proxy has {len(actual_timestamps)} video frames; "
            f"expected {timeline.frame_count}"
        )
    maximum_pts_error = 0.0
    for index, (actual, expected) in enumerate(
        zip(actual_timestamps, timeline.timestamps_sec)
    ):
        error = abs(actual - expected)
        maximum_pts_error = max(maximum_pts_error, error)
        if error > base.PTS_TOLERANCE_SEC:
            raise base.BuildError(
                f"generated frame {index} PTS {actual:.9f} differs from "
                f"timeline {expected:.9f} by {error:.9f}s"
            )

    format_info = stream_value.get("format")
    if not isinstance(format_info, dict):
        raise base.BuildError("generated proxy has no format metadata")
    container_duration = float(format_info.get("duration", 0.0))
    # MP4 format duration is quantized independently from the already verified
    # frame PTS.  Some VFR files report it a few milliseconds before the final
    # frame PTS, even though that frame is present and exact.  Permit up to one
    # final-frame duration while still rejecting a materially truncated file.
    duration_slack = max(timeline.frame_durations_sec[-1], 0.01)
    if container_duration + duration_slack < timeline.visual_end_sec:
        raise base.BuildError(
            f"generated proxy ends at {container_duration:.6f}s before "
            f"last frame PTS {timeline.visual_end_sec:.6f}s"
        )
    if include_audio:
        expected_audio_end = max(0.0, audio.start_sec) + audio.duration_sec
        if container_duration + 0.05 < expected_audio_end:
            raise base.BuildError(
                f"generated proxy ends at {container_duration:.6f}s before "
                f"audio end {expected_audio_end:.6f}s"
            )
    return {
        "container_duration_sec": container_duration,
        "video": video,
        "audio": audios[0] if include_audio else None,
        "frame_count": len(actual_timestamps),
        "first_frame_pts_sec": actual_timestamps[0],
        "last_frame_pts_sec": actual_timestamps[-1],
        "maximum_frame_pts_error_sec": maximum_pts_error,
    }


def multiview_cache_matches(
    *,
    output_paths: Mapping[str, Path],
    manifest_path: Path,
    cache_key_sha256: str,
) -> bool:
    if set(output_paths) != set(VIEW_ORDER) or not manifest_path.is_file():
        return False
    if any(not output_paths[view].is_file() for view in VIEW_ORDER):
        return False
    try:
        manifest = base.load_json_object(manifest_path)
    except base.BuildError:
        return False
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("cache_key_sha256") != cache_key_sha256
        or manifest.get("master_view") != MASTER_VIEW
        or manifest.get("view_order") != list(VIEW_ORDER)
    ):
        return False
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != set(VIEW_ORDER):
        return False
    for view in VIEW_ORDER:
        path = output_paths[view]
        item = outputs.get(view)
        if not isinstance(item, dict):
            return False
        expected_size = item.get("size_bytes")
        expected_hash = item.get("sha256")
        if (
            item.get("path") != str(path.resolve())
            or not isinstance(expected_size, int)
            or path.stat().st_size != expected_size
            or not isinstance(expected_hash, str)
            or base.sha256_file(path) != expected_hash
        ):
            return False
    return True


def _options_dict(options: base.BuildOptions) -> dict[str, Any]:
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
        "video_track_timescale": base.VIDEO_TIME_BASE_HZ,
        "movflags": ["faststart"],
        "layout": "four_independent_streams",
        "audio_view": MASTER_VIEW,
    }


def build_multiview_proxy(args: argparse.Namespace) -> dict[str, Any]:
    source_videos = _resolve_source_videos(args)
    timeline_path = args.timeline.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    manifest_path = (
        args.manifest.expanduser().resolve()
        if args.manifest is not None
        else output_dir / "review_multiview.manifest.json"
    )
    output_paths = {
        view: (output_dir / OUTPUT_FILENAMES[view]).resolve()
        for view in VIEW_ORDER
    }
    if manifest_path in output_paths.values():
        raise base.BuildError("manifest path collides with a video output")
    for path in (*source_videos.values(), timeline_path):
        if not path.is_file():
            raise base.BuildError(f"input file does not exist: {path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    existing = [
        path
        for path in (*output_paths.values(), manifest_path)
        if path.exists()
    ]
    if existing and not args.reuse:
        raise base.BuildError(
            "one or more outputs already exist; pass --reuse to reuse a valid "
            "bundle or atomically rebuild a stale bundle"
        )

    options = base.BuildOptions(
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
    _validate_options(options)
    timeline = base.load_timeline(timeline_path, args.source_bag)
    mcap_path = base.locate_mcap(timeline.source_bag)
    metadata_path = timeline.source_bag / "metadata.yaml"
    ffmpeg_info = base.executable_and_version(args.ffmpeg)
    ffprobe_info = base.executable_and_version(args.ffprobe)
    ffmpeg = ffmpeg_info["path"]
    ffprobe = ffprobe_info["path"]

    base._emit("fingerprinting_inputs")
    inputs: dict[str, Any] = {
        "timeline": base.input_fingerprint(timeline_path),
        "source_mcap": base.input_fingerprint(mcap_path),
    }
    for view in VIEW_ORDER:
        inputs[f"{view}_avi"] = base.input_fingerprint(source_videos[view])
    if metadata_path.is_file():
        inputs["source_bag_metadata"] = base.input_fingerprint(metadata_path)
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
        "master_view": MASTER_VIEW,
        "view_order": list(VIEW_ORDER),
    }
    cache_key_sha256 = base.sha256_value(cache_basis)
    if args.reuse and multiview_cache_matches(
        output_paths=output_paths,
        manifest_path=manifest_path,
        cache_key_sha256=cache_key_sha256,
    ):
        base._emit(
            "reused",
            output_dir=str(output_dir),
            manifest=str(manifest_path),
            cache_key_sha256=cache_key_sha256,
        )
        return base.load_json_object(manifest_path)

    base._emit("validating_source_videos")
    source_video_probe = validate_source_videos(
        ffprobe=ffprobe,
        source_videos=source_videos,
        frame_count=timeline.frame_count,
    )
    base._emit("extracting_audio")
    audio = base.extract_audio_from_mcap(
        source_bag=timeline.source_bag,
        timeline=timeline,
        audio_info_topic=options.audio_info_topic,
        audio_pcm_topic=options.audio_pcm_topic,
    )
    concat_durations = base.quantize_durations_for_time_base(
        timeline.timestamps_sec,
        timeline.frame_durations_sec[-1],
    )
    media_probes: dict[str, dict[str, Any]] = {}
    command_templates: dict[str, dict[str, list[str]]] = {}
    temporary_outputs: dict[str, Path] = {}

    with tempfile.TemporaryDirectory(
        dir=output_dir,
        prefix=".review_multiview.build.",
    ) as temporary_name:
        work_dir = Path(temporary_name)
        wav_path = work_dir / "audio.wav"
        base.write_pcm_wav(wav_path, audio)
        for view in VIEW_ORDER:
            view_dir = work_dir / view
            frames_dir = view_dir / "frames"
            frames_dir.mkdir(parents=True)
            frames_pattern = frames_dir / "%06d.jpg"
            concat_path = view_dir / "frames.ffconcat"
            temporary_proxy = view_dir / OUTPUT_FILENAMES[view]
            include_audio = view == MASTER_VIEW

            extract_command = single_view_frame_extraction_command(
                ffmpeg=ffmpeg,
                source_avi=source_videos[view],
                frames_pattern=frames_pattern,
                timeline=timeline,
                options=options,
            )
            base._emit(
                "rendering_independent_frames",
                view=view,
                frame_count=timeline.frame_count,
            )
            base._run_checked(
                extract_command,
                f"{view} independent frame extraction",
            )
            frame_paths = sorted(frames_dir.glob("*.jpg"))
            if len(frame_paths) != timeline.frame_count:
                raise base.BuildError(
                    f"{view} rendered {len(frame_paths)} frames; "
                    f"expected {timeline.frame_count}"
                )
            base.create_ffconcat(
                concat_path,
                frame_paths,
                concat_durations,
            )
            encode_command = independent_proxy_encoding_command(
                ffmpeg=ffmpeg,
                concat_path=concat_path,
                wav_path=wav_path,
                output_path=temporary_proxy,
                timeline=timeline,
                audio=audio,
                options=options,
                include_audio=include_audio,
            )
            base._emit("encoding_independent_proxy", view=view)
            base._run_checked(encode_command, f"{view} proxy encoding")
            base._emit("verifying_independent_proxy", view=view)
            media_probes[view] = probe_independent_output(
                ffprobe=ffprobe,
                proxy_path=temporary_proxy,
                timeline=timeline,
                audio=audio,
                options=options,
                include_audio=include_audio,
            )
            with temporary_proxy.open("rb") as stream:
                os.fsync(stream.fileno())
            replacements = {
                str(work_dir): "{work_dir}",
                str(temporary_proxy): f"{{output_{view}}}",
            }
            command_templates[view] = {
                "frame_extraction": base._command_template(
                    extract_command,
                    replacements,
                ),
                "proxy_encoding": base._command_template(
                    encode_command,
                    replacements,
                ),
            }
            temporary_outputs[view] = temporary_proxy

        # Replace every stream first and publish the manifest last.  A stopped
        # build can leave new video files but never a falsely valid manifest.
        for view in VIEW_ORDER:
            os.replace(temporary_outputs[view], output_paths[view])

    outputs: dict[str, Any] = {}
    for view in VIEW_ORDER:
        path = output_paths[view]
        outputs[view] = {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "sha256": base.sha256_file(path),
            "has_audio": view == MASTER_VIEW,
            "media_probe": media_probes[view],
        }
    maximum_pts_error = max(
        float(media_probes[view]["maximum_frame_pts_error_sec"])
        for view in VIEW_ORDER
    )
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "builder_version": BUILDER_VERSION,
        "case_id": timeline.case_id,
        "cache_key_sha256": cache_key_sha256,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_path": str(manifest_path),
        "master_view": MASTER_VIEW,
        "view_order": list(VIEW_ORDER),
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
            "timestamp_quantization_time_base_hz": base.VIDEO_TIME_BASE_HZ,
            "gaps": list(timeline.gaps),
            "segments": list(timeline.segments),
            "maximum_pts_error_sec": maximum_pts_error,
        },
        "audio": {
            "view": MASTER_VIEW,
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
        "outputs": outputs,
    }
    base.atomic_write_json(manifest_path, manifest)
    base._emit(
        "complete",
        output_dir=str(output_dir),
        manifest=str(manifest_path),
        outputs={
            view: {
                "path": outputs[view]["path"],
                "sha256": outputs[view]["sha256"],
            }
            for view in VIEW_ORDER
        },
        duration_sec=media_probes[MASTER_VIEW]["container_duration_sec"],
    )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build four independent, CAM4-timeline-aligned VFR review streams "
            "for CAM4, FLIR, CAM2, and CAM3."
        )
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        help=(
            "Case media directory containing cam_4/rgb.avi, flir/rgb.avi, "
            "cam_2/rgb.avi, and cam_3/rgb.avi."
        ),
    )
    parser.add_argument("--cam4-avi", type=Path)
    parser.add_argument("--flir-avi", type=Path)
    parser.add_argument("--cam2-avi", type=Path)
    parser.add_argument("--cam3-avi", type=Path)
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument(
        "--source-bag",
        type=Path,
        help="Override timeline.source_bag.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Defaults to OUTPUT_DIR/review_multiview.manifest.json.",
    )
    parser.add_argument(
        "--reuse",
        action="store_true",
        help=(
            "Reuse only when all four output hashes, inputs, tools, and "
            "options match; otherwise rebuild the bundle."
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
        default=base.DEFAULT_AUDIO_INFO_TOPIC,
    )
    parser.add_argument(
        "--audio-pcm-topic",
        default=base.DEFAULT_AUDIO_PCM_TOPIC,
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        build_multiview_proxy(args)
    except (base.BuildError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
