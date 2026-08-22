import asyncio
import json
from types import SimpleNamespace
import threading
import wave

import numpy as np
import pytest

import integration_debug.asr_runtime as asr_runtime
from integration_debug.asr_runtime import (
    Pcm16MonoResampler,
    _select_input_format,
    validate_websocket_url,
)


class FakeInputStream:
    def __init__(self, owner, **kwargs) -> None:
        self.owner = owner
        self.device = 0 if kwargs["device"] is None else int(kwargs["device"])
        self.samplerate = int(kwargs["samplerate"])
        self.channels = int(kwargs["channels"])
        self.blocksize = int(kwargs["blocksize"])
        self.callback = kwargs["callback"]
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True


class FakeSoundDevice:
    def __init__(self, supported_formats, *, name="USB Native Stereo") -> None:
        self.supported_formats = set(supported_formats)
        self.default = SimpleNamespace(device=(0, 0))
        self.info = {
            "name": name,
            "max_input_channels": 2,
            "default_samplerate": 48_000.0,
        }
        self.checked = []
        self.streams = []

    def query_devices(self, device=None, kind=None):
        if device is None and kind is None:
            return [dict(self.info)]
        return dict(self.info)

    def check_input_settings(self, *, device, channels, dtype, samplerate) -> None:
        assert dtype == "int16"
        candidate = (int(samplerate), int(channels))
        self.checked.append(candidate)
        if candidate not in self.supported_formats:
            raise RuntimeError("unsupported test format")

    def InputStream(self, **kwargs):
        assert (int(kwargs["samplerate"]), int(kwargs["channels"])) in self.supported_formats
        stream = FakeInputStream(self, **kwargs)
        self.streams.append(stream)
        return stream


class FakeAsrWsClient:
    instances = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.fed = []
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True

    def feed(self, pcm: bytes) -> None:
        self.fed.append(pcm)

    def stop(self, *, flush_timeout_sec: float = 4.0) -> None:
        del flush_timeout_sec
        self.stopped = True

    def stats(self):
        return {
            "sent_chunks": 0,
            "responses": 0,
            "dropped_chunks": 0,
            "sessions": 0,
            "padded_final_bytes": 0,
            "pending_chunks": 0,
            "connected": False,
        }


class FakeResponseSocket:
    def __init__(self, responses) -> None:
        self._responses = iter(responses)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._responses)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeSendSocket:
    def __init__(self, *, fail=False) -> None:
        self.fail = fail
        self.sent = []

    async def send(self, value) -> None:
        if self.fail:
            raise RuntimeError("test send failure")
        self.sent.append(value)


def test_websocket_url_rejects_all_inline_credential_channels() -> None:
    assert validate_websocket_url("wss://asr.example.test/v1") == (
        "wss://asr.example.test/v1"
    )

    secret = "do-not-log-this-token"
    for value in (
        f"wss://asr.example.test/v1?token={secret}",
        f"wss://asr.example.test/v1#{secret}",
        "wss://asr.example.test/v1?",
        "wss://asr.example.test/v1#",
        f"wss://user:{secret}@asr.example.test/v1",
    ):
        with pytest.raises(ValueError) as raised:
            validate_websocket_url(value)
        assert secret not in str(raised.value)


def test_zip_transport_config_uses_server_vad_and_handoff_keywords() -> None:
    client = asr_runtime.AsrWsClient(
        url="wss://asr.example.test/v1",
        websockets_module=object(),
        on_final=lambda _text: None,
        on_partial=lambda _text: None,
        on_connection=lambda _connected: None,
        on_error=lambda _message: None,
    )

    config = client._config()["config"]

    assert config["use_vad"] is True
    assert config["use_timestamp"] is False
    keywords = {row["keyword"]: row["sensitivity"] for row in config["keywords"]}
    assert keywords["Malleable"] == 8
    assert keywords["직접 교시"] == 9


def test_final_response_reports_uncorrelated_latest_pcm_interval(monkeypatch) -> None:
    finals = []
    metadata = []
    errors = []
    client = asr_runtime.AsrWsClient(
        url="wss://asr.example.test/v1",
        websockets_module=object(),
        on_final=finals.append,
        on_final_metadata=metadata.append,
        on_partial=lambda _text: None,
        on_connection=lambda _connected: None,
        on_error=errors.append,
    )
    client._last_audio_sent_monotonic_ns = 10_000_000_000
    monkeypatch.setattr(asr_runtime.time, "monotonic_ns", lambda: 10_275_400_000)
    socket = FakeResponseSocket(
        [json.dumps({"partial": "Kelly please", "is_final": 1})]
    )

    asyncio.run(client._receiver(socket))

    assert finals == ["Kelly please"]
    assert metadata == [
        {
            "response_latency_ms": 275.4,
            "latency_basis": "latest_pcm_send_complete_to_final_receive",
            "latency_correlated": False,
        }
    ]
    assert errors == []
    assert client.stats()["responses"] == 1


def test_final_before_any_pcm_preserves_single_argument_callback(monkeypatch) -> None:
    finals = []
    metadata = []
    client = asr_runtime.AsrWsClient(
        url="wss://asr.example.test/v1",
        websockets_module=object(),
        on_final=finals.append,
        on_final_metadata=metadata.append,
        on_partial=lambda _text: None,
        on_connection=lambda _connected: None,
        on_error=lambda _message: None,
    )
    monkeypatch.setattr(asr_runtime.time, "monotonic_ns", lambda: 12_000_000_000)

    asyncio.run(
        client._receiver(
            FakeResponseSocket(
                [json.dumps({"partial": "Bovie please", "is_final": 1})]
            )
        )
    )

    assert finals == ["Bovie please"]
    assert metadata[0]["response_latency_ms"] is None
    assert metadata[0]["latency_correlated"] is False


def test_pcm_timestamp_updates_only_after_successful_send(monkeypatch) -> None:
    client = asr_runtime.AsrWsClient(
        url="wss://asr.example.test/v1",
        websockets_module=object(),
        on_final=lambda _text: None,
        on_partial=lambda _text: None,
        on_connection=lambda _connected: None,
        on_error=lambda _message: None,
    )
    monkeypatch.setattr(asr_runtime.time, "monotonic_ns", lambda: 42_000_000)

    async def send_once(socket) -> None:
        client._queue = asyncio.Queue()
        client._queue.put_nowait(b"\x00" * asr_runtime.CHUNK_BYTES)
        client._stopping = True
        await client._sender(socket)

    successful = FakeSendSocket()
    asyncio.run(send_once(successful))
    assert client._last_audio_sent_monotonic_ns == 42_000_000

    client._last_audio_sent_monotonic_ns = 0
    failing = FakeSendSocket(fail=True)
    with pytest.raises(RuntimeError, match="test send failure"):
        asyncio.run(send_once(failing))
    assert client._last_audio_sent_monotonic_ns == 0


def test_input_format_falls_back_to_native_stereo() -> None:
    sounddevice = FakeSoundDevice({(48_000, 2)})

    selected, info = _select_input_format(sounddevice, 0)

    assert selected.sample_rate == 48_000
    assert selected.channels == 2
    assert selected.block_frames == 4_800
    assert selected.requires_conversion
    assert info["name"] == "USB Native Stereo"
    assert sounddevice.checked == [(16_000, 1), (48_000, 1), (48_000, 2)]


def test_input_format_keeps_direct_16khz_mono_when_supported() -> None:
    sounddevice = FakeSoundDevice({(16_000, 1), (48_000, 2)})

    selected, _info = _select_input_format(sounddevice, 0)

    assert selected.sample_rate == 16_000
    assert selected.channels == 1
    assert selected.block_frames == 1_600
    assert not selected.requires_conversion
    assert sounddevice.checked == [(16_000, 1)]


def test_wpctl_properties_create_ubuntu_logical_input(monkeypatch) -> None:
    output = """
id 111, type PipeWire:Interface:Node
    audio.channels = "1"
    device.icon-name = "audio-card-analog"
  * media.class = "Audio/Source"
  * node.nick = "Shure MVX2U GEN 2"
"""
    monkeypatch.setattr(
        asr_runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=output),
    )

    source = asr_runtime._query_pipewire_default_source()

    assert source == {
        "name": "Analog Input - Shure MVX2U GEN 2",
        "input_channels": 1,
    }


def test_wpctl_distinguishes_reachable_graph_with_no_input(monkeypatch) -> None:
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command == ["wpctl", "status"]:
            return SimpleNamespace(stdout="Audio\n Sources:\n")
        raise asr_runtime.subprocess.CalledProcessError(3, command)

    monkeypatch.setattr(asr_runtime.subprocess, "run", fake_run)

    with pytest.raises(asr_runtime.AudioInputUnavailable) as exc_info:
        asr_runtime._query_pipewire_default_source()

    assert exc_info.value.status == asr_runtime.DEVICE_STATUS_NO_INPUT
    assert "no PipeWire microphone input" in str(exc_info.value)
    assert calls == [
        ["wpctl", "inspect", asr_runtime.PIPEWIRE_DEFAULT_SOURCE],
        ["wpctl", "status"],
    ]


def test_wpctl_reports_unreachable_host_audio_graph(monkeypatch) -> None:
    def fake_run(command, **_kwargs):
        if command == ["wpctl", "status"]:
            raise asr_runtime.subprocess.CalledProcessError(3, command)
        raise asr_runtime.subprocess.CalledProcessError(3, command)

    monkeypatch.setattr(asr_runtime.subprocess, "run", fake_run)

    with pytest.raises(asr_runtime.AudioInputUnavailable) as exc_info:
        asr_runtime._query_pipewire_default_source()

    assert exc_info.value.status == asr_runtime.DEVICE_STATUS_HOST_AUDIO_UNAVAILABLE
    assert "not reachable" in str(exc_info.value)


def test_runtime_exposes_only_pipewire_default_input(monkeypatch, tmp_path) -> None:
    sounddevice = FakeSoundDevice({(16_000, 1)}, name="default")
    monkeypatch.setattr(
        asr_runtime,
        "_optional_audio_modules",
        lambda: (np, sounddevice, object(), ""),
    )
    monkeypatch.setattr(
        asr_runtime,
        "_query_pipewire_default_source",
        lambda: {
            "name": "Analog Input - Shure MVX2U GEN 2",
            "input_channels": 1,
        },
    )

    runtime = asr_runtime.AsrMicrophoneRuntime(
        default_url="wss://asr.example.test/v1",
        topic="/sensors/surgeon/sentence",
        output_dir=tmp_path,
    )

    assert runtime.snapshot()["devices"] == [
        {
            "id": 0,
            "name": "Analog Input - Shure MVX2U GEN 2",
            "input_channels": 1,
            "default_samplerate": 48_000.0,
            "default": True,
        }
    ]
    assert runtime.snapshot()["device_status"] == "READY"
    assert "Shure MVX2U GEN 2" in runtime.snapshot()["device_message"]


def test_runtime_treats_reachable_pipewire_with_no_input_as_device_state(
    monkeypatch,
    tmp_path,
) -> None:
    sounddevice = FakeSoundDevice({(16_000, 1)}, name="default")
    monkeypatch.setattr(
        asr_runtime,
        "_optional_audio_modules",
        lambda: (np, sounddevice, object(), ""),
    )
    monkeypatch.setattr(
        asr_runtime,
        "_query_pipewire_default_source",
        lambda: (_ for _ in ()).throw(
            asr_runtime.AudioInputUnavailable(
                asr_runtime.DEVICE_STATUS_NO_INPUT,
                "Ubuntu currently exposes no PipeWire microphone input",
            )
        ),
    )

    runtime = asr_runtime.AsrMicrophoneRuntime(
        default_url="wss://asr.example.test/v1",
        topic="/sensors/surgeon/sentence",
        output_dir=tmp_path,
    )

    snapshot = runtime.snapshot()
    assert snapshot["available"] is True
    assert snapshot["state"] == "STOPPED"
    assert snapshot["devices"] == []
    assert snapshot["device_status"] == "NO_INPUT"
    assert "no PipeWire microphone input" in snapshot["device_message"]
    assert snapshot["last_error"] == ""


def test_runtime_fails_closed_when_portaudio_exposes_raw_alsa(monkeypatch, tmp_path) -> None:
    sounddevice = FakeSoundDevice({(16_000, 1)}, name="USB Audio (hw:1,0)")
    monkeypatch.setattr(
        asr_runtime,
        "_optional_audio_modules",
        lambda: (np, sounddevice, object(), ""),
    )
    monkeypatch.setattr(
        asr_runtime,
        "_query_pipewire_default_source",
        lambda: {"name": "Analog Input - Shure MVX2U GEN 2", "input_channels": 1},
    )

    runtime = asr_runtime.AsrMicrophoneRuntime(
        default_url="wss://asr.example.test/v1",
        topic="/sensors/surgeon/sentence",
        output_dir=tmp_path,
    )

    snapshot = runtime.snapshot()
    assert snapshot["devices"] == []
    assert snapshot["device_status"] == "BRIDGE_ERROR"
    assert "not connected to Ubuntu PipeWire" in snapshot["last_error"]


def test_runtime_rejects_hidden_raw_portaudio_device(monkeypatch, tmp_path) -> None:
    sounddevice = FakeSoundDevice({(16_000, 1)}, name="default")
    monkeypatch.setattr(
        asr_runtime,
        "_optional_audio_modules",
        lambda: (np, sounddevice, object(), ""),
    )
    monkeypatch.setattr(
        asr_runtime,
        "_query_pipewire_default_source",
        lambda: {"name": "Analog Input - Test Mic", "input_channels": 1},
    )
    runtime = asr_runtime.AsrMicrophoneRuntime(
        default_url="wss://asr.example.test/v1",
        topic="/sensors/surgeon/sentence",
        output_dir=tmp_path,
    )

    with pytest.raises(ValueError, match="current Ubuntu input"):
        runtime.start(device_id=9)


def test_resampler_downmixes_native_stereo_to_16khz_mono() -> None:
    block = np.empty((480, 2), dtype=np.int16)
    block[:, 0] = 1_000
    block[:, 1] = 3_000
    converter = Pcm16MonoResampler(
        np,
        input_sample_rate=48_000,
        input_channels=2,
    )

    converted = np.frombuffer(converter.process(block), dtype="<i2")

    assert converted.shape == (160,)
    assert np.all(converted == 2_000)


def test_resampler_is_continuous_across_irregular_callback_boundaries() -> None:
    frame_count = 1_003
    left = np.linspace(-20_000, 20_000, frame_count, dtype=np.int16)
    right = np.linspace(12_000, -8_000, frame_count, dtype=np.int16)
    source = np.column_stack((left, right))

    whole_converter = Pcm16MonoResampler(
        np,
        input_sample_rate=44_100,
        input_channels=2,
    )
    split_converter = Pcm16MonoResampler(
        np,
        input_sample_rate=44_100,
        input_channels=2,
    )
    whole = whole_converter.process(source)
    split = b"".join(
        split_converter.process(source[start:end])
        for start, end in ((0, 73), (73, 74), (74, 288), (288, 701), (701, 1_003))
    )

    assert split == whole


def test_runtime_uses_native_capture_but_feeds_and_records_wire_format(
    monkeypatch,
    tmp_path,
) -> None:
    sounddevice = FakeSoundDevice({(48_000, 2)}, name="default")
    FakeAsrWsClient.instances = []
    monkeypatch.setattr(
        asr_runtime,
        "_optional_audio_modules",
        lambda: (np, sounddevice, object(), ""),
    )
    monkeypatch.setattr(asr_runtime, "AsrWsClient", FakeAsrWsClient)
    monkeypatch.setattr(
        asr_runtime,
        "_query_pipewire_default_source",
        lambda: {"name": "Analog Input - Test Mic", "input_channels": 1},
    )
    runtime = asr_runtime.AsrMicrophoneRuntime(
        default_url="wss://asr.example.test/v1",
        topic="/sensors/surgeon/sentence",
        output_dir=tmp_path,
    )

    runtime.start(device_id=0)
    snapshot = runtime.snapshot()
    assert snapshot["sample_rate"] == 16_000
    assert snapshot["channels"] == 1
    assert snapshot["input_sample_rate"] == 48_000
    assert snapshot["input_channels"] == 2
    assert snapshot["input_block_frames"] == 4_800
    assert snapshot["resampling"] is True

    native_block = np.empty((480, 2), dtype=np.int16)
    native_block[:, 0] = 1_000
    native_block[:, 1] = 3_000
    sounddevice.streams[0].callback(native_block, 480, None, None)
    client = FakeAsrWsClient.instances[0]
    assert len(client.fed) == 1
    assert len(client.fed[0]) == 160 * 2
    assert np.all(np.frombuffer(client.fed[0], dtype="<i2") == 2_000)
    client.kwargs["on_final_metadata"](
        {
            "response_latency_ms": 184.2,
            "latency_basis": "latest_pcm_send_complete_to_final_receive",
            "latency_correlated": False,
        }
    )
    client.kwargs["on_final"]("Alice and mass")
    final = runtime.snapshot()["finals"][-1]
    assert final["text"] == "Allis and 메스"
    assert final["postprocess_corrections"] == 2
    assert final["response_latency_ms"] == 184.2
    assert final["latency_correlated"] is False

    runtime.close()
    stopped = runtime.snapshot()
    with wave.open(stopped["recording_path"], "rb") as recording:
        assert recording.getframerate() == 16_000
        assert recording.getnchannels() == 1
        assert recording.getsampwidth() == 2
        assert recording.getnframes() == 160
    assert sounddevice.streams[0].closed
    assert client.stopped
    assert stopped["connected"] is False


def test_concurrent_stop_cannot_be_undone_by_inflight_start(
    monkeypatch,
    tmp_path,
) -> None:
    sounddevice = FakeSoundDevice({(48_000, 2)}, name="default")
    probe_entered = threading.Event()
    release_probe = threading.Event()
    original_check = sounddevice.check_input_settings

    def delayed_check(**kwargs) -> None:
        probe_entered.set()
        assert release_probe.wait(timeout=2)
        original_check(**kwargs)

    sounddevice.check_input_settings = delayed_check
    FakeAsrWsClient.instances = []
    monkeypatch.setattr(
        asr_runtime,
        "_optional_audio_modules",
        lambda: (np, sounddevice, object(), ""),
    )
    monkeypatch.setattr(asr_runtime, "AsrWsClient", FakeAsrWsClient)
    monkeypatch.setattr(
        asr_runtime,
        "_query_pipewire_default_source",
        lambda: {"name": "Analog Input - Test Mic", "input_channels": 1},
    )
    runtime = asr_runtime.AsrMicrophoneRuntime(
        default_url="wss://asr.example.test/v1",
        topic="/sensors/surgeon/sentence",
        output_dir=tmp_path,
    )

    start_thread = threading.Thread(target=runtime.start, kwargs={"device_id": 0})
    start_thread.start()
    assert probe_entered.wait(timeout=2)
    stop_thread = threading.Thread(target=runtime.stop_async)
    stop_thread.start()
    assert stop_thread.is_alive()
    release_probe.set()
    start_thread.join(timeout=2)
    stop_thread.join(timeout=2)
    assert not start_thread.is_alive()
    assert not stop_thread.is_alive()
    assert runtime.close()

    assert runtime.snapshot()["state"] == "STOPPED"
    assert sounddevice.streams[0].closed


def test_capture_lock_prevents_two_runtimes_and_is_released_on_stop(
    monkeypatch,
    tmp_path,
) -> None:
    sounddevice = FakeSoundDevice({(16_000, 1)}, name="default")
    FakeAsrWsClient.instances = []
    monkeypatch.setattr(
        asr_runtime,
        "_optional_audio_modules",
        lambda: (np, sounddevice, object(), ""),
    )
    monkeypatch.setattr(asr_runtime, "AsrWsClient", FakeAsrWsClient)
    monkeypatch.setattr(
        asr_runtime,
        "_query_pipewire_default_source",
        lambda: {"name": "Analog Input - Test Mic", "input_channels": 1},
    )
    lock_path = tmp_path / "shared" / "microphone.lock"
    first = asr_runtime.AsrMicrophoneRuntime(
        default_url="wss://asr.example.test/v1",
        topic="/sensors/surgeon/sentence",
        output_dir=tmp_path / "first",
        capture_lock_path=lock_path,
    )
    second = asr_runtime.AsrMicrophoneRuntime(
        default_url="wss://asr.example.test/v1",
        topic="/sensors/surgeon/sentence",
        output_dir=tmp_path / "second",
        capture_lock_path=lock_path,
    )

    first.start(device_id=0)
    with pytest.raises(RuntimeError, match="already owned"):
        second.start(device_id=0)

    assert first.close()
    second.start(device_id=0)
    assert second.snapshot()["state"] == "LISTENING"
    assert second.close()


def test_capture_lock_is_released_when_start_fails(monkeypatch, tmp_path) -> None:
    sounddevice = FakeSoundDevice({(16_000, 1)}, name="default")

    class StartFailClient(FakeAsrWsClient):
        def start(self) -> None:
            raise RuntimeError("websocket setup failed")

    monkeypatch.setattr(
        asr_runtime,
        "_optional_audio_modules",
        lambda: (np, sounddevice, object(), ""),
    )
    monkeypatch.setattr(asr_runtime, "AsrWsClient", StartFailClient)
    monkeypatch.setattr(
        asr_runtime,
        "_query_pipewire_default_source",
        lambda: {"name": "Analog Input - Test Mic", "input_channels": 1},
    )
    lock_path = tmp_path / "microphone.lock"
    failed = asr_runtime.AsrMicrophoneRuntime(
        default_url="wss://asr.example.test/v1",
        topic="/sensors/surgeon/sentence",
        output_dir=tmp_path / "failed",
        capture_lock_path=lock_path,
    )

    with pytest.raises(RuntimeError, match="websocket setup failed"):
        failed.start(device_id=0)

    monkeypatch.setattr(asr_runtime, "AsrWsClient", FakeAsrWsClient)
    replacement = asr_runtime.AsrMicrophoneRuntime(
        default_url="wss://asr.example.test/v1",
        topic="/sensors/surgeon/sentence",
        output_dir=tmp_path / "replacement",
        capture_lock_path=lock_path,
    )
    replacement.start(device_id=0)
    assert replacement.close()


def test_operational_mode_does_not_save_wav_or_transcript(
    monkeypatch,
    tmp_path,
) -> None:
    sounddevice = FakeSoundDevice({(16_000, 1)}, name="default")
    FakeAsrWsClient.instances = []
    monkeypatch.setattr(
        asr_runtime,
        "_optional_audio_modules",
        lambda: (np, sounddevice, object(), ""),
    )
    monkeypatch.setattr(asr_runtime, "AsrWsClient", FakeAsrWsClient)
    monkeypatch.setattr(
        asr_runtime,
        "_query_pipewire_default_source",
        lambda: {"name": "Analog Input - Test Mic", "input_channels": 1},
    )
    output_dir = tmp_path / "operational-artifacts"
    runtime = asr_runtime.AsrMicrophoneRuntime(
        default_url="wss://asr.example.test/v1",
        topic="/sensors/surgeon/sentence",
        output_dir=output_dir,
        save_artifacts=False,
        capture_lock_path=tmp_path / "microphone.lock",
    )

    runtime.start(device_id=0)
    block = np.full((1_600, 1), 2_000, dtype=np.int16)
    sounddevice.streams[-1].callback(block, 1_600, None, None)
    FakeAsrWsClient.instances[-1].kwargs["on_final"]("Bovie please")
    assert runtime.close()

    snapshot = runtime.snapshot()
    assert snapshot["artifacts_enabled"] is False
    assert snapshot["recording_path"] == ""
    assert snapshot["transcript_path"] == ""
    assert not output_dir.exists()
