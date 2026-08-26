#!/usr/bin/env python3
"""Line-delimited JSON worker that keeps Supertonic resident."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import traceback


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--voice", default="F1")
    parser.add_argument("--language", default="ko")
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--speed", type=float, default=1.05)
    parser.add_argument("--silence-duration", type=float, default=0.3)
    parser.add_argument("--intra-op-threads", type=int, default=4)
    parser.add_argument("--inter-op-threads", type=int, default=1)
    return parser.parse_args()


class ResidentRuntime:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self._numpy = None
        self._tts = None
        self._style = None

    def _load(self) -> None:
        if self._tts is not None:
            return
        import numpy as np
        from supertonic import TTS

        tts = TTS(
            model="supertonic-3",
            model_dir=self.args.model_dir,
            auto_download=False,
            intra_op_num_threads=self.args.intra_op_threads,
            inter_op_num_threads=self.args.inter_op_threads,
        )
        self._numpy = np
        self._tts = tts
        self._style = tts.get_voice_style(self.args.voice)

    def synthesize(self, text: str, output_path: Path, seed: int) -> dict[str, object]:
        self._load()
        assert self._numpy is not None
        assert self._tts is not None
        self._numpy.random.seed(seed)
        waveform, _sdk_duration = self._tts.synthesize(
            text=text,
            voice_style=self._style,
            total_steps=self.args.steps,
            speed=self.args.speed,
            max_chunk_length=None,
            silence_duration=self.args.silence_duration,
            lang=self.args.language,
            verbose=False,
        )
        if waveform.ndim != 2 or waveform.shape[0] != 1 or waveform.shape[1] <= 0:
            raise RuntimeError(f"invalid waveform shape: {waveform.shape}")
        if not self._numpy.isfinite(waveform).all():
            raise RuntimeError("waveform contains non-finite samples")
        peak = float(self._numpy.max(self._numpy.abs(waveform)))
        if peak <= 1e-5:
            raise RuntimeError("waveform is effectively silent")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._tts.save_audio(waveform, str(output_path))
        return {
            "ok": True,
            "audio_duration_sec": waveform.shape[-1] / float(self._tts.sample_rate),
        }


def main() -> int:
    args = parse_args()
    # Keep the JSON protocol on a duplicate of the original stdout. Library
    # diagnostics are redirected to stderr so they cannot corrupt responses.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    sys.stdout = sys.stderr
    runtime = ResidentRuntime(args)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("command") == "shutdown":
                return 0
            if request.get("command") != "synthesize":
                raise ValueError("unsupported command")
            response = runtime.synthesize(
                str(request["text"]),
                Path(str(request["output_path"])),
                int(request["seed"]),
            )
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            response = {"ok": False, "error": str(exc) or exc.__class__.__name__}
        protocol.write(json.dumps(response, ensure_ascii=False) + "\n")
        protocol.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
