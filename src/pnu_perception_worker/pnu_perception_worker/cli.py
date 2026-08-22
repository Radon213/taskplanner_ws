"""CLI entry point for the PNU perception worker."""

from __future__ import annotations

import argparse
import os

import uvicorn

from .app import build_engine, create_app
from .config import WorkerConfig


def _parse_args() -> argparse.Namespace:
    defaults = WorkerConfig.from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=defaults.host)
    parser.add_argument("--port", type=int, default=defaults.port)
    parser.add_argument("--log-level", default=os.environ.get("PNU_LOG_LEVEL", "info"))
    parsed = parser.parse_args()
    parsed.config = defaults
    return parsed


def main() -> None:
    args = _parse_args()
    api_token = args.config.read_api_token()
    args.config.validate_bind_auth(args.host, api_token)
    app = create_app(build_engine(args.config), api_token=api_token)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
