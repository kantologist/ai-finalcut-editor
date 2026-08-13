"""Native macOS / desktop shell around the local web UI."""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from typing import Any

from .paths import APP_NAME, ensure_app_home


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(host: str, port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Desktop UI server did not start on {host}:{port}")


def _start_server(host: str, port: int) -> Any:
    import uvicorn

    from .webapp.app import create_app

    config = uvicorn.Config(
        create_app(),
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="ai-edit-uvicorn", daemon=True)
    thread.start()
    return server


def run_desktop(*, host: str = "127.0.0.1", port: int | None = None) -> int:
    """Launch the FastAPI UI inside a native WebKit window."""
    ensure_app_home()

    try:
        import webview
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "Desktop mode requires pywebview. Install with:\n"
            "  uv sync --extra desktop\n"
            "or: pip install 'ai-finalcut-editor[desktop]'"
        ) from exc

    bind_port = port or _free_port()
    _start_server(host, bind_port)
    _wait_for_server(host, bind_port)

    url = f"http://{host}:{bind_port}/"
    window = webview.create_window(
        APP_NAME,
        url=url,
        width=1280,
        height=860,
        min_size=(960, 640),
        background_color="#1a1714",
    )
    _ = window
    webview.start(debug=False)
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ai-edit-desktop", description=f"Open {APP_NAME}")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 = pick a free local port")
    args = parser.parse_args(argv)
    port = None if not args.port else args.port
    raise SystemExit(run_desktop(host=args.host, port=port))


if __name__ == "__main__":
    main()
