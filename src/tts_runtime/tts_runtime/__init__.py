"""Durable, idempotent local TTS playback for Taskplanner."""

from .core import (
    DeterministicWavCache,
    EnqueueResult,
    PlaybackDispatcher,
    PlaybackEvent,
    PlaybackResult,
    PlaybackStore,
    PrewarmResult,
    ReplyRequest,
    RuntimeConfig,
    SynthesisResult,
    supertonic_model_manifest_sha256,
)
from .correlation import AuthoritativeTimingCorrelator

__all__ = [
    "AuthoritativeTimingCorrelator",
    "DeterministicWavCache",
    "EnqueueResult",
    "PlaybackDispatcher",
    "PlaybackEvent",
    "PlaybackResult",
    "PlaybackStore",
    "PrewarmResult",
    "ReplyRequest",
    "RuntimeConfig",
    "SynthesisResult",
    "supertonic_model_manifest_sha256",
]
