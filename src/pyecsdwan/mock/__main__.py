"""Run the bundled fake Orchestrator standalone: ``python -m pyecsdwan.mock``."""

from __future__ import annotations

import argparse

import uvicorn

from pyecsdwan.mock.server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m pyecsdwan.mock",
        description="Bundled fake Orchestrator for pyecsdwan demos and e2e tests.",
    )
    parser.add_argument(
        "--port", type=int, default=8442, help="TCP port to listen on (default: 8442)"
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="address to bind (default: 127.0.0.1)"
    )
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
