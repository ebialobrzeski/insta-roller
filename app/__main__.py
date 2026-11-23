"""python -m app [--browser | --no-browser] [--port 8756] [--host 127.0.0.1] [--data DIR]
python -m app --install-shortcut | --uninstall-shortcut

Starts the local web server and the job runner in this one process and shows the UI in the
application's own window. --browser: in the default browser instead. --no-browser: nowhere.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import traceback
import urllib.request
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


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    """The options, with `mode`: window (the default), browser or headless."""
    from .web import default_data_dir

    p = argparse.ArgumentParser(prog="python -m app", description="insta-roller: job queue, runner and UI in one application")
    p.add_argument("--host", default="127.0.0.1",
                   help="address to listen on; anything but 127.0.0.1 lets other devices in, and there is no login")
    p.add_argument("--port", type=int, default=PORT, help=f"port (default {PORT}; another free one when it is taken)")
    p.add_argument("--data", default=str(default_data_dir()), help="folder for the queue, the settings and the log")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--browser", action="store_true", help="no window: open the UI in the default browser")
    where.add_argument("--no-browser", action="store_true", help="no window and no browser: only the server and the runner")
    where.add_argument("--install-shortcut", action="store_true", help="put insta-roller in the Start menu, then exit")
    where.add_argument("--uninstall-shortcut", action="store_true", help="remove it from the Start menu, then exit")
    args = p.parse_args(argv)
    args.mode = "headless" if args.no_browser else "browser" if args.browser else "window"
    return args


def show_running(url: str, mode: str, post=None) -> str:
    """A second start: bring the running app forward. Returns what was done: window, browser or nothing."""
    if not url or mode == "headless":
        return "nothing"
    if mode == "window":
        try:
            (post or (lambda u: urllib.request.urlopen(urllib.request.Request(u, b"", method="POST"), timeout=5).read()))(
                f"{url}api/window/show")
            return "window"
        except Exception:                       # the running one has no window (--browser, --no-browser)
            pass
    webbrowser.open(url)
    return "browser"


def keep_log(data: Path) -> None:
    """Under pythonw there is no console: what would be printed goes to <data>/app.log."""
    if sys.stdout is not None and sys.stderr is not None:
        for stream in (sys.stdout, sys.stderr):     # folder names the console cannot show must not stop a job
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(errors="replace")
        return
    data.mkdir(parents=True, exist_ok=True)
    path = data / "app.log"
    if path.exists() and path.stat().st_size > 2_000_000:       # one earlier log is kept
        path.replace(data / "app.log.1")
    sys.stdout = sys.stderr = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} insta-roller starts")


def run(args: argparse.Namespace) -> None:
    import uvicorn

    from .runner import Runner
    from .web import create_app

    data = Path(args.data).resolve()
    if args.install_shortcut or args.uninstall_shortcut:
        from . import desktop
        own = "" if Path(args.data) == Path(parse([]).data) else f' --data "{data}"'
        if args.install_shortcut:
            print(f"Shortcut created: {desktop.install_shortcut(data, arguments='-m app' + own)}")
        else:
            print("Shortcut removed" if desktop.uninstall_shortcut() else "There was no shortcut")
        return

    mode = args.mode
    held = lock(data)
    address = data / "app.url"
    if held is None:
        url = address.read_text(encoding="utf-8").strip() if address.exists() else ""
        print(f"insta-roller is already running{': ' + url if url else ''} (data folder {data}); shown in: {show_running(url, mode)}")
        return

    if mode == "window":
        from . import desktop
        why = desktop.unavailable()
        if why:                                 # still usable: the same UI in the browser
            print(f"No application window: {why}. Opening the browser instead.")
            if sys.__stdout__ is None:
                desktop.message_box(f"Okno aplikacji nie jest dostępne ({why}).\nInterfejs otworzy się w przeglądarce.", 0x30)
            mode = "browser"

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

    def started() -> bool:
        while not server.started:
            if server.should_exit:
                return False
            time.sleep(0.1)
        address.write_text(url, encoding="utf-8")
        print(f"insta-roller: {url}   (data: {data})" + ("   Ctrl+C stops it" if mode != "window" else ""))
        return True

    runner.start()
    try:
        if mode == "window":
            serving = threading.Thread(target=server.run, name="server", daemon=True)
            serving.start()
            if started():
                # `?okno` tells the page it is inside the application window (see static/ui.js).
                desktop.run_window(f"http://127.0.0.1:{port}/?okno=1", data, app.state.queue,
                                   on_ready=lambda show: setattr(app.state, "show_window", show))
            server.should_exit = True
            serving.join(10)
        else:
            def opened() -> None:
                if started() and mode == "browser":
                    webbrowser.open(url)

            threading.Thread(target=opened, daemon=True).start()
            server.run()                        # until Ctrl+C
    except KeyboardInterrupt:                   # uvicorn hands the Ctrl+C on after its own shutdown
        pass
    finally:
        print("insta-roller: stopping")
        runner.stop()                           # kills the running step's process tree; the job resumes next time
        address.unlink(missing_ok=True)
        held.close()


def main(argv: list[str] | None = None) -> None:
    args = parse(argv)
    windowless = sys.stdout is None or sys.stderr is None
    keep_log(Path(args.data).resolve())
    try:
        run(args)
    except Exception:
        traceback.print_exc()
        if windowless:                          # nobody sees the log unless told
            from .desktop import message_box
            message_box("insta-roller nie mógł się uruchomić.\n\nSzczegóły są w pliku:\n"
                        f"{Path(args.data).resolve() / 'app.log'}", 0x10)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
