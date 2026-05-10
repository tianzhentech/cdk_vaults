"""Command-line entry points for local development."""

import argparse
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    load_dotenv(project_root / ".env")

    parser = argparse.ArgumentParser(description="Run CDK Vaults")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--reload", action="store_true")
    parser.add_argument(
        "--timeout-graceful-shutdown",
        type=int,
        default=int(os.environ.get("GRACEFUL_SHUTDOWN_TIMEOUT", "3")),
    )
    args = parser.parse_args()

    uvicorn.run(
        "server.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        app_dir=str(project_root),
        timeout_graceful_shutdown=args.timeout_graceful_shutdown,
    )
