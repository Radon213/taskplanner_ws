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
        self.local_onsets = []
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True

    def feed(
        self,
        pcm: bytes,
        *,
        captured_monotonic_ns: int | None = None,
    ) -> None:
        del captured_monotonic_ns
        self.fed.append(pcm)

    def note_local_audio_onset(
        self,
        captured_monotonic_ns: int,
        *,
        dbfs: float,
        threshold_dbfs: float,
    ) -> None:
        self.local_onsets.append(
            (captured_monotonic_ns, dbfs, threshold_dbfs)
        )

    def stop(self, *, flush_timeout_sec: float = 4.0) -> None:
        del flush_timeout_sec
        self.stopped = True

    def stats(self):
        return {
            "sent_chunks": 0,
            "responses": 0,
            "dropped_chunks": 0,
            "ingress_dropped_chunks": 0,
            "stale_ingress_dropped_chunks": 0,
            "queue_dropped_chunks": 0,
            "stale_queue_dropped_chunks": 0,
            "pcm_ingress_max_age_ms": (
                asr_runtime.DEFAULT_PCM_INGRESS_MAX_AGE_SEC * 1_000.0
            ),
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


class FakeSessionSocket(FakeSendSocket):
    def __init__(self, responses=()) -> None:
        super().__init__()
        self._responses = iter(responses)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._responses)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeWebSocketContext:
    def __init__(self, socket) -> None:
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, _exc_type, _exc, _traceback) -> bool:
        return False


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


def test_pcm_callback_ingress_uses_one_scheduled_drain_and_drops_stale_audio() -> None:
    """A busy event loop must not accumulate one callback per audio block."""

    client = asr_runtime.AsrWsClient(
        url="wss://asr.example.test/v1",
        websockets_module=object(),
        on_final=lambda _text: None,
        on_partial=lambda _text: None,
        on_connection=lambda _connected: None,
        on_error=lambda _message: None,
        queue_max=8,
        ingress_max_age_sec=0.05,
    )

    class Loop:
        def __init__(self) -> None:
            self.callbacks = []

        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, callback) -> None:
            self.callbacks.append(callback)

    loop = Loop()
    client._loop = loop
    client._queue = asyncio.Queue(maxsize=8)
    client._ready.set()
    now_ns = asr_runtime.time.monotonic_ns()

    client.feed(b"stale", captured_monotonic_ns=now_ns - 100_000_000)
    client.feed(b"fresh", captured_monotonic_ns=now_ns)

    # The second PortAudio callback only appends; the originally queued drain
    # will take both blocks together when the event loop gets CPU time.
    assert len(loop.callbacks) == 1
    loop.callbacks[0]()

    assert client._queue.get_nowait()[1] == b"fresh"
    assert client._queue.empty()
    stats = client.stats()
    assert stats["stale_ingress_dropped_chunks"] == 1
    assert stats["pending_chunks"] == 0


def test_default_pcm_ingress_keeps_ten_seconds_before_capacity_eviction() -> None:
    """The default count cap must not preempt the ten-second stale policy."""

    client = asr_runtime.AsrWsClient(
        url="wss://asr.example.test/v1",
        websockets_module=object(),
        on_final=lambda _text: None,
        on_partial=lambda _text: None,
        on_connection=lambda _connected: None,
        on_error=lambda _message: None,
    )

    class Loop:
        def __init__(self) -> None:
            self.callbacks = []

        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, callback) -> None:
            self.callbacks.append(callback)

    loop = Loop()
    client._loop = loop
    client._queue = asyncio.Queue(maxsize=client._queue_max)
    client._ready.set()
    now_ns = asr_runtime.time.monotonic_ns()

    # 100 callback blocks at the normal 100 ms cadence span 9.9 seconds.
    for index in range(100):
        client.feed(
            bytes([index]),
            captured_monotonic_ns=now_ns - (99 - index) * 100_000_000,
        )

    assert client._queue_max >= 100
    assert len(loop.callbacks) == 1
    loop.callbacks[0]()
    assert client._queue.qsize() == 100
    assert client.stats()["dropped_chunks"] == 0


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
            "last_changed_partial_to_final_ms": None,
        }
    ]
    assert errors == []
    assert client.stats()["responses"] == 1


def test_final_response_reports_last_changed_partial_to_final_interval(monkeypatch) -> None:
    finals = []
    metadata = []
    partials = []
    client = asr_runtime.AsrWsClient(
        url="wss://asr.example.test/v1",
        websockets_module=object(),
        on_final=finals.append,
        on_final_metadata=metadata.append,
        on_partial=partials.append,
        on_connection=lambda _connected: None,
        on_error=lambda _message: None,
    )
    client._last_audio_sent_monotonic_ns = 9_000_000_000
    timestamps = iter((10_000_000_000, 10_275_400_000))
    monkeypatch.setattr(asr_runtime.time, "monotonic_ns", lambda: next(timestamps))

    asyncio.run(
        client._receiver(
            FakeResponseSocket(
                [
                    json.dumps({"partial": "Kelly", "is_final": 0}),
                    json.dumps({"partial": "Kelly please", "is_final": 1}),
                ]
            )
        )
    )

    assert partials == ["Kelly"]
    assert finals == ["Kelly please"]
    assert metadata[0]["last_changed_partial_to_final_ms"] == 275.4


def test_first_changed_partial_reports_approximate_local_onset_latency(monkeypatch) -> None:
    partials = []
    client = asr_runtime.AsrWsClient(
        url="wss://asr.example.test/v1",
        websockets_module=object(),
        on_final=lambda _text: None,
        on_partial=partials.append,
        on_connection=lambda _connected: None,
        on_error=lambda _message: None,
    )
    client.note_local_audio_onset(
        10_000_000_000,
        dbfs=-21.4,
        threshold_dbfs=-45.0,
    )
    monkeypatch.setattr(asr_runtime.time, "monotonic_ns", lambda: 10_612_300_000)

    asyncio.run(
        client._receiver(
            FakeResponseSocket([json.dumps({"partial": "Kelly", "is_final": 0})])
        )
    )

    assert partials == ["Kelly"]
    stats = client.stats()
    assert stats["local_onset_to_first_partial_ms"] == 612.3
    assert stats["local_onset_basis"] == (
        "audio_callback_dbfs_threshold_crossing_approximate"
    )
    assert stats["local_onset_dbfs"] == -21.4
    assert stats["local_onset_threshold_dbfs"] == -45.0


def test_opt_in_server_final_rollover_preserves_subchunk_pcm_without_eof() -> None:
    client = asr_runtime.AsrWsClient(
        url="wss://asr.example.test/v1",
        websockets_module=None,
        on_final=lambda _text: None,
        on_partial=lambda _text: None,
        on_connection=lambda _connected: None,
        on_error=lambda _message: None,
        rollover_after_final=True,
    )
    socket = FakeSessionSocket(
        [json.dumps({"partial": "Kelly please", "is_final": 1})]
    )

    class OneSessionWebsockets:
        def connect(self, *_args, **_kwargs):
            return FakeWebSocketContext(socket)

    client._websockets = OneSessionWebsockets()

    async def run_session() -> bytes:
        queued = b"queued-after-final"
        client._queue = asyncio.Queue()
        client._queue.put_nowait(queued)
        client._send_buffer.extend(b"previous-remainder")
        await client._session()
        retained = bytes(client._send_buffer)
        while not client._queue.empty():
            retained += client._queue.get_nowait()
        return retained

    retained = asyncio.run(run_session())

    assert retained == b"previous-remainderqueued-after-final"
    assert client.stats()["normal_rollovers"] == 1
    assert not any(
        isinstance(value, str) and '"eof"' in value for value in socket.sent
    )


def test_empty_server_final_keeps_the_existing_session() -> None:
    client = asr_runtime.AsrWsClient(
        url="wss://asr.example.test/v1",
        websockets_module=object(),
        on_final=lambda _text: None,
        on_partial=lambda _text: None,
        on_connection=lambda _connected: None,
        on_error=lambda _message: None,
    )

    async def receive_empty_final() -> bool:
        rollover_requested = asyncio.Event()
        await client._receiver(
            FakeResponseSocket([json.dumps({"partial": "", "is_final": 1})]),
            rollover_requested,
        )
        return rollover_requested.is_set()

    assert asyncio.run(receive_empty_final()) is False


def test_nonempty_server_final_keeps_the_existing_session_by_default() -> None:
    client = asr_runtime.AsrWsClient(
        url="wss://asr.example.test/v1",
        websockets_module=object(),
        on_final=lambda _text: None,
        on_partial=lambda _text: None,
        on_connection=lambda _connected: None,
        on_error=lambda _message: None,
    )

    async def receive_final() -> bool:
        rollover_requested = asyncio.Event()
        await client._receiver(
            FakeResponseSocket([json.dumps({"partial": "Kelly please", "is_final": 1})]),
            rollover_requested,
        )
        return rollover_requested.is_set()

    assert asyncio.run(receive_final()) is False


def test_opt_in_normal_final_opens_next_session_without_reconnect_backoff(monkeypatch) -> None:
    client = asr_runtime.AsrWsClient(
        url="wss://asr.example.test/v1",
        websockets_module=None,
        on_final=lambda _text: None,
        on_partial=lambda _text: None,
        on_connection=lambda _connected: None,
        on_error=lambda _message: None,
        rollover_after_final=True,
    )
    first = FakeSessionSocket(
        [json.dumps({"partial": "Kelly please", "is_final": 1})]
    )
    second = FakeSessionSocket()

    class TwoSessionWebsockets:
        def __init__(self) -> None:
            self.calls = 0

        def connect(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return FakeWebSocketContext(first)
            if self.calls == 2:
                client._stopping = True
                return FakeWebSocketContext(second)
            raise AssertionError("unexpected third ASR session")

    web = TwoSessionWebsockets()
    client._websockets = web
    sleeps = []

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asr_runtime.asyncio, "sleep", record_sleep)

    asyncio.run(client._run())

    assert web.calls == 2
    assert client.stats()["normal_rollovers"] == 1
    assert sleeps == []


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
    assert sounddevice.checked == [(16_000, 2), (16_000, 1), (48_000, 2)]


def test_input_format_prefers_direct_16khz_stereo_when_supported() -> None:
    sounddevice = FakeSoundDevice({(16_000, 1), (16_000, 2), (48_000, 2)})

    selected, _info = _select_input_format(sounddevice, 0)

    assert selected.sample_rate == 16_000
    assert selected.channels == 2
    assert selected.block_frames == 1_600
    assert selected.requires_conversion
    assert sounddevice.checked == [(16_000, 2)]


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
        if command == ["wpctl", "status", "--name"]:
            return SimpleNamespace(stdout="Audio\n Sources:\n")
        raise asr_runtime.subprocess.CalledProcessError(3, command)

    monkeypatch.setattr(asr_runtime.subprocess, "run", fake_run)

    with pytest.raises(asr_runtime.AudioInputUnavailable) as exc_info:
        asr_runtime._query_pipewire_default_source()

    assert exc_info.value.status == asr_runtime.DEVICE_STATUS_NO_INPUT
    assert "no PipeWire microphone input" in str(exc_info.value)
    assert calls == [
        ["wpctl", "inspect", asr_runtime.PIPEWIRE_DEFAULT_SOURCE],
        ["wpctl", "status", "--name"],
    ]


def test_wpctl_reports_unreachable_host_audio_graph(monkeypatch) -> None:
    def fake_run(command, **_kwargs):
        if command == ["wpctl", "status", "--name"]:
            raise asr_runtime.subprocess.CalledProcessError(3, command)
        raise asr_runtime.subprocess.CalledProcessError(3, command)

    monkeypatch.setattr(asr_runtime.subprocess, "run", fake_run)

    with pytest.raises(asr_runtime.AudioInputUnavailable) as exc_info:
        asr_runtime._query_pipewire_default_source()

    assert exc_info.value.status == asr_runtime.DEVICE_STATUS_HOST_AUDIO_UNAVAILABLE
    assert "not reachable" in str(exc_info.value)


def test_wpctl_resolves_selected_source_when_alias_points_to_sink(monkeypatch) -> None:
    sink_output = """
id 59, type PipeWire:Interface:Node
  * media.class = \"Audio/Sink\"
"""
    status_output = """
Audio
 ├─ Sources:
 │      62. alsa_input.usb-Test.HiFi__Line__source [vol: 1.00]
 │      63. alsa_input.usb-Test.HiFi__Mic__source [vol: 1.00]
Settings
 └─ Default Configured Devices:
         1. Audio/Source  alsa_input.usb-Test.HiFi__Mic__source
"""
    source_output = """
id 63, type PipeWire:Interface:Node
    audio.channels = \"2\"
  * media.class = \"Audio/Source\"
  * node.nick = \"USB Audio Microphone\"
"""
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command == ["wpctl", "inspect", asr_runtime.PIPEWIRE_DEFAULT_SOURCE]:
            return SimpleNamespace(stdout=sink_output)
        if command == ["wpctl", "status", "--name"]:
            return SimpleNamespace(stdout=status_output)
        if command == ["wpctl", "inspect", "63"]:
            return SimpleNamespace(stdout=source_output)
        raise AssertionError(command)

    monkeypatch.setattr(asr_runtime.subprocess, "run", fake_run)

    source = asr_runtime._query_pipewire_default_source()

    assert source == {
        "name": "Input - USB Audio Microphone",
        "input_channels": 2,
    }
    assert calls == [
        ["wpctl", "inspect", asr_runtime.PIPEWIRE_DEFAULT_SOURCE],
        ["wpctl", "status", "--name"],
        ["wpctl", "inspect", "63"],
    ]


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


def test_resampler_uses_the_active_channel_when_the_other_is_silent() -> None:
    block = np.empty((480, 2), dtype=np.int16)
    block[:, 0] = 0
    block[:, 1] = 3_000
    converter = Pcm16MonoResampler(
        np,
        input_sample_rate=48_000,
        input_channels=2,
    )

    converted = np.frombuffer(converter.process(block), dtype="<i2")

    assert converted.shape == (160,)
    assert np.all(converted == 3_000)


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
    assert snapshot["local_onset_threshold_dbfs"] == -45.0
    assert snapshot["local_onset_basis"] == (
        "audio_callback_dbfs_threshold_crossing_approximate"
    )

    native_block = np.empty((480, 2), dtype=np.int16)
    native_block[:, 0] = 1_000
    native_block[:, 1] = 3_000
    sounddevice.streams[0].callback(native_block, 480, None, None)
    client = FakeAsrWsClient.instances[0]
    assert client.kwargs["rollover_after_final"] is False
    assert len(client.fed) == 1
    assert len(client.fed[0]) == 160 * 2
    assert np.all(np.frombuffer(client.fed[0], dtype="<i2") == 2_000)
    assert len(client.local_onsets) == 1
    assert client.local_onsets[0][2] == -45.0
    client.kwargs["on_final_metadata"](
        {
            "response_latency_ms": 184.2,
            "latency_basis": "latest_pcm_send_complete_to_final_receive",
            "latency_correlated": False,
        }
    )
    client.kwargs["on_final"]("Alice and mass")
    final = runtime.snapshot()["finals"][-1]
    # The command lane uses the same canonical final as the postprocess
    # diagnostic field, so every downstream consumer receives one text.
    assert final["text"] == "Allis and 메스"
    assert final["raw_text"] == "Alice and mass"
    assert final["corrected_text"] == "Allis and 메스"
    assert final["postprocess_corrections"] == 2
    assert final["postprocess_command_correction_pairs"] == (
        ("Alice", "Allis"),
        ("mass", "메스"),
    )
    assert final["postprocess_applied_to_command"] is True
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


def test_artifact_recording_keeps_a_bounded_newest_pcm_window(monkeypatch, tmp_path) -> None:
    """Long recordings must not grow the ASR owner heap for an entire run."""

    sounddevice = FakeSoundDevice({(48_000, 2)}, name="default")
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
        recording_max_seconds=1.0,
    )

    with runtime._lock:
        runtime._recording_active = True
        # Five 0.25 second callback windows exceed the one-second cap.
        for _ in range(5):
            runtime._append_recording_pcm(b"\x01\x00" * 4_000)

    snapshot = runtime.snapshot()
    assert snapshot["recording_max_sec"] == 1.0
    assert snapshot["recording_buffered_sec"] <= 1.0
    assert snapshot["recording_dropped_sec"] == 0.25
    assert snapshot["recording_truncated"] is True
    assert [event["type"] for event in runtime.drain_events()].count(
        "asr_recording_window_truncated"
    ) == 1


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
