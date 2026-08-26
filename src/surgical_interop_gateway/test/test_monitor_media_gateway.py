from pathlib import Path

from surgical_interop_gateway.monitor_media_gateway import (
    EncoderSettings,
    LatestFrameMailbox,
    build_ffmpeg_command,
)


def test_ffmpeg_command_is_bounded_webos_h264_hls(tmp_path: Path) -> None:
    command = build_ffmpeg_command(
        EncoderSettings(
            output_root=tmp_path,
            width=1280,
            height=720,
            frame_rate=5,
            bitrate_kbps=2400,
        )
    )
    joined = " ".join(command)
    assert "libx264" in command
    assert "yuv420p" in command
    assert "-profile:v main" in joined
    assert "-hls_time 1" in joined
    assert "-hls_list_size 3" in joined
    assert "delete_segments+independent_segments+omit_endlist+temp_file" in command
    assert str(tmp_path / "flir.m3u8") == command[-1]


def test_latest_frame_mailbox_replaces_backlog() -> None:
    mailbox = LatestFrameMailbox()
    assert mailbox.submit(b"one") is False
    assert mailbox.submit(b"two") is True
    assert mailbox.take(0.0) == b"two"
    assert mailbox.take(0.0) is None
    mailbox.close()


def test_ffmpeg_command_clamps_unsafe_dimensions_and_bitrate(tmp_path: Path) -> None:
    command = build_ffmpeg_command(
        EncoderSettings(
            output_root=tmp_path,
            width=99999,
            height=1,
            frame_rate=999,
            bitrate_kbps=999999,
        )
    )
    joined = " ".join(command)
    assert "scale=3840:180" in joined
    assert "-r 60" in joined
    assert "-b:v 40000k" in joined
