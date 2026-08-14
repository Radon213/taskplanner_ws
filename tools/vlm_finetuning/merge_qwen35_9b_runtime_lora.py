#!/usr/bin/env python3
"""Merge a selected Qwen3.5-9B LoRA into a standalone BF16 checkpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-length", type=int, default=8192)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    adapter = args.adapter.resolve()
    output_dir = args.output_dir.resolve()
    if not (adapter / "adapter_config.json").is_file():
        raise FileNotFoundError(f"adapter_config.json is missing: {adapter}")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty output: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    import unsloth
    import torch
    from unsloth import FastVisionModel

    model, processor = FastVisionModel.from_pretrained(
        model_name=str(adapter),
        max_seq_length=args.max_length,
        dtype=torch.bfloat16,
        load_in_4bit=False,
        load_in_16bit=True,
        full_finetuning=False,
        fast_inference=False,
        local_files_only=True,
    )
    model.save_pretrained_merged(
        str(output_dir),
        processor,
        save_method="merged_16bit",
    )
    files = {
        path.name: {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for path in sorted(output_dir.iterdir())
        if path.is_file()
    }
    manifest = {
        "schema": "taskplanner.qwen35_9b_runtime_merged_model.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "adapter": str(adapter),
        "adapter_config_sha256": sha256_file(adapter / "adapter_config.json"),
        "adapter_model_sha256": sha256_file(adapter / "adapter_model.safetensors"),
        "dtype": "bfloat16",
        "max_length": args.max_length,
        "unsloth": getattr(unsloth, "__version__", "unknown"),
        "files": files,
    }
    (output_dir / "taskplanner_merge_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
