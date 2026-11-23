"""python -m app [--no-browser] [--port 8756] [--host 127.0.0.1] [--data DIR]

Starts the local web server and the job runner in this one process and opens the UI.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

PORT = 8756


def free_port(host: str, port: int) -> int:
    """`port` when it is free on `host`, else a free one picked by the system."""
    for candidate in (port, 0):
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as probe:
            try:
                probe.bind((host, candidate))
                return probe.getsockname()[1]
            except OSError:
                continue
    raise SystemExit(f"No free port on {host}")


def lock(data: Path):
    """One app per data folder: two runners on one queue would run the same job twice.
    Returns the open lock file, or None when another app holds it."""
    data.mkdir(parents=True, exist_ok=True)
    handle = open(data / "app.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    from .runner import Runner
    from .web import create_app, default_data_dir

    p = argparse.ArgumentParser(prog="python -m app", description="insta-roller: job queue, runner and browser UI")
    p.add_argument("--host", default="127.0.0.1",
                   help="address to listen on; anything but 127.0.0.1 lets other devices in, and there is no login")
    p.add_argument("--port", type=int, default=PORT, help=f"port (default {PORT}; another free one when it is taken)")
    p.add_argument("--data", default=str(default_data_dir()), help="folder for the queue and the settings")
    p.add_argument("--no-browser", action="store_true", help="do not open the UI in the browser")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):     # folder names the console cannot show must not stop a job
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    data = Path(args.data).resolve()
    held = lock(data)
    address = data / "app.url"
    if held is None:
        url = address.read_text(encoding="utf-8").strip() if address.exists() else ""
        print(f"insta-roller is already running{': ' + url if url else ''} (data folder {data})")
        if url and not args.no_browser:
            webbrowser.open(url)
        return

    port = free_port(args.host, args.port)
    if port != args.port:
        print(f"Port {args.port} is taken; using {port}")
    local = args.host in ("127.0.0.1", "localhost", "::1")
    url = f"http://{'127.0.0.1' if local or args.host in ('0.0.0.0', '::') else args.host}:{port}/"
    if not local:
        print(f"WARNING: listening on {args.host}: there is no login, anyone who can reach this port can use the app.")

    app = create_app(data)
    runner = Runner(app.state.queue, work=app.state.work)

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):      # Ctrl+C: no new step starts while the server winds down
            runner.closing.set()
            super().handle_exit(sig, frame)

    server = Server(uvicorn.Config(app, host=args.host, port=port, log_level="warning"))

    def opened() -> None:
        while not server.started:
            if server.should_exit:
                return
            time.sleep(0.1)
        address.write_text(url, encoding="utf-8")
        print(f"insta-roller: {url}   (data: {data})   Ctrl+C stops it")
        if not args.no_browser:
            webbrowser.open(url)

    runner.start()
    threading.Thread(target=opened, daemon=True).start()
    try:
        server.run()                            # until Ctrl+C
    except KeyboardInterrupt:                   # uvicorn hands the Ctrl+C on after its own shutdown
        pass
    finally:
        print("insta-roller: stopping")
        runner.stop()                           # kills the running step's process tree; the job resumes next time
        address.unlink(missing_ok=True)
        held.close()


if __name__ == "__main__":
    main()
