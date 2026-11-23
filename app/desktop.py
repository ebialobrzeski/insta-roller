"""The application window: the same UI in a native window (WebView2 through pywebview), a tray
icon, the system's folder dialog, a Start menu shortcut.

pywebview and pystray are imported only when the window is really opened, so everything else here
(and the tests) works without a display.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path

from .steps import python_exe
from .web import REPO, activity_text

TITLE = "insta-roller"
SIZE, MIN_SIZE = (1320, 900), (420, 560)
ACCENT = (250, 93, 25)
SHORTCUT = "insta-roller.lnk"


# --- small Windows helpers -------------------------------------------------------------------
def message_box(text: str, flags: int = 0x40) -> int:
    """A system message box (the app has no console). Returns the button: 6 yes, 7 no, 2 cancel."""
    if os.name != "nt":
        print(text)
        return 0
    return ctypes.windll.user32.MessageBoxW(None, text, TITLE, flags | 0x10000)      # MB_SETFOREGROUND


def button_names() -> list[str]:
    """Yes, No, Cancel as this Windows writes them on a message box (they follow the system's language)."""
    try:
        get = ctypes.windll.user32.MB_GetString
        get.restype = ctypes.c_wchar_p
        names = [(get(index) or "").replace("&", "") for index in (5, 6, 1)]
        if all(names):
            return names
    except (AttributeError, OSError):
        pass
    return ["Tak", "Nie", "Anuluj"]


def ask_on_close() -> str:
    """The question when the window is closed while a film is being made."""
    yes, no, cancel = button_names()
    answer = message_box("Film jeszcze powstaje.\n\n"
                         f"{yes} – pracuj dalej w tle. Okno schowa się do ikony przy zegarze.\n"
                         f"{no} – zatrzymaj pracę i zamknij. Film dokończysz po ponownym uruchomieniu.\n"
                         f"{cancel} – wróć do aplikacji.", 0x03 | 0x20)                 # MB_YESNOCANCEL | MB_ICONQUESTION
    return {6: "hide", 7: "quit"}.get(answer, "stay")


def close_decision(busy: bool, ask=ask_on_close) -> str:
    """What closing the window does: "quit" when nothing is running; otherwise the user chooses
    "hide" (keep working, in the tray), "quit" (stop) or "stay" (do not close)."""
    if not busy:
        return "quit"
    answer = ask()
    return answer if answer in ("hide", "quit") else "stay"


def webview2_version() -> str:
    """The installed WebView2 runtime, or "" when there is none (the window needs it)."""
    if os.name != "nt":
        return ""
    import winreg
    key = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for root, path in ((winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\{key}"), (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\{key}"),
                       (winreg.HKEY_CURRENT_USER, rf"Software\{key}")):
        try:
            with winreg.OpenKey(root, path) as handle:
                version = str(winreg.QueryValueEx(handle, "pv")[0])
                if version and version != "0.0.0.0":
                    return version
        except OSError:
            pass
    return ""


def unavailable() -> str:
    """Why the window cannot be opened here; "" when it can."""
    if os.name != "nt":
        return "the application window is made for Windows"
    try:
        import webview  # noqa: F401
    except Exception as error:
        return f"pywebview is not installed ({error}); run: pip install -e .[app]"
    if not webview2_version():
        return "the Microsoft Edge WebView2 runtime is not installed"
    return ""


# --- what the page may ask the window for ---------------------------------------------------------
def explorer_command(path: Path) -> list[str]:
    """Explorer with a file selected, or with a folder opened."""
    path = Path(path)
    return ["explorer.exe", str(path)] if path.is_dir() else ["explorer.exe", "/select,", str(path)]


class Api:
    """The functions the page calls as window.pywebview.api.<name>(...). Paths only: nothing is run."""

    def __init__(self, window=None, launch=subprocess.Popen):
        self._window, self._launch = window, launch      # underscore: not offered to the page

    def choose_folder(self, start: str = "") -> str:
        """The system's folder dialog. The chosen folder, or "" when the dialog was cancelled."""
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        start = start if start and os.path.isdir(start) else ""
        chosen = self._window.create_file_dialog(kind, directory=start)
        if not chosen:
            return ""
        return str(chosen if isinstance(chosen, (str, os.PathLike)) else chosen[0])

    def reveal(self, path: str) -> bool:
        """Show a film (selected) or a folder in Explorer. False when it is not there."""
        target = Path(path)
        if not target.is_absolute() or not target.exists():
            return False
        self._launch(explorer_command(target))
        return True


# --- the window's size and place, remembered in the data folder -------------------------------------
def load_geometry(data: Path, screens: list[tuple[int, int, int, int]] | None = None) -> dict:
    """{width, height, x, y}; x and y are None (let Windows place it) unless the remembered spot is on a screen."""
    saved = {}
    try:
        saved = json.loads((Path(data) / "window.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        width, height = max(int(saved["width"]), MIN_SIZE[0]), max(int(saved["height"]), MIN_SIZE[1])
        x, y = int(saved["x"]), int(saved["y"])
    except (KeyError, TypeError, ValueError):
        return {"width": SIZE[0], "height": SIZE[1], "x": None, "y": None}
    visible = any(sx - 50 <= x <= sx + sw - 200 and sy - 50 <= y <= sy + sh - 100 for sx, sy, sw, sh in screens or [])
    return {"width": width, "height": height, "x": x if visible else None, "y": y if visible else None}


def save_geometry(data: Path, width: int, height: int, x: int, y: int) -> None:
    if width < MIN_SIZE[0] or height < MIN_SIZE[1] or x <= -30000:      # minimised: keep what was there
        return
    (Path(data) / "window.json").write_text(json.dumps({"width": width, "height": height, "x": x, "y": y}), encoding="utf-8")


# --- the mark as an icon -----------------------------------------------------------------------------
def mark_image(size: int = 64):
    """The glider over the horizon, white on the accent colour."""
    from PIL import Image, ImageDraw
    scale = 4                                           # drawn large, then reduced: smooth lines
    s = size * scale
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([0, 0, s - 1, s - 1], radius=s // 5, fill=(*ACCENT, 255))
    u, pen = s / 28, max(int(s / 13), 1)                # the same 28-unit drawing as the page's mark
    white = (255, 255, 255, 255)
    draw.line([(4 * u, 13 * u), (14 * u, 9.5 * u), (24 * u, 13 * u)], fill=white, width=pen, joint="curve")    # wings
    draw.line([(14 * u, 5.5 * u), (14 * u, 16 * u)], fill=white, width=pen)                                    # fuselage
    draw.line([(11.5 * u, 16 * u), (16.5 * u, 16 * u)], fill=white, width=pen)                                 # tail
    draw.arc([4 * u, 19 * u, 14 * u, 25 * u], 200, 340, fill=white, width=max(pen * 2 // 3, 1))                # horizon
    draw.arc([14 * u, 18 * u, 24 * u, 24 * u], 20, 160, fill=white, width=max(pen * 2 // 3, 1))
    return image.resize((size, size), Image.LANCZOS)


def write_icon(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mark_image(256).save(path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    return path


# --- Start menu shortcut ----------------------------------------------------------------------------
def start_menu() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def pythonw_exe() -> str:
    """The windowless interpreter next to this one: the app then has no console."""
    here = Path(sys.executable)
    candidate = here.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else here)


class _ShellLink:
    """A .lnk file through the system's own IShellLinkW (Unicode paths, unlike WScript.Shell)."""
    METHODS = {"path": (3, 20), "description": (6, 7), "folder": (8, 9), "arguments": (10, 11), "icon": (16, 17)}     # get, set

    def __init__(self):
        from ctypes import wintypes
        self.ole = ctypes.OleDLL("ole32")
        self.ole.CoInitialize(None)
        ids = [(ctypes.c_byte * 16)() for _ in range(3)]
        for raw, text in zip(ids, ("{00021401-0000-0000-C000-000000000046}", "{000214F9-0000-0000-C000-000000000046}",
                                   "{0000010B-0000-0000-C000-000000000046}")):       # ShellLink, IShellLinkW, IPersistFile
            self.ole.CLSIDFromString(ctypes.c_wchar_p(text), raw)
        self.link, self.file = ctypes.c_void_p(), ctypes.c_void_p()
        self.ole.CoCreateInstance(ids[0], None, 1, ids[1], ctypes.byref(self.link))
        self._method(self.link, 0, ctypes.c_void_p, ctypes.c_void_p)(self.link, ids[2], ctypes.byref(self.file))
        self.int, self.text = ctypes.c_int, wintypes.LPCWSTR

    @staticmethod
    def _method(obj, index: int, *argtypes):
        table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(table[index])

    def set(self, name: str, value: str) -> None:
        index = self.METHODS[name][1]
        if name == "icon":
            self._method(self.link, index, self.text, self.int)(self.link, value, 0)
        else:
            self._method(self.link, index, self.text)(self.link, value)

    def get(self, name: str) -> str:
        index, buffer = self.METHODS[name][0], ctypes.create_unicode_buffer(1024)
        if name == "path":
            self._method(self.link, index, ctypes.c_wchar_p, self.int, ctypes.c_void_p, ctypes.c_uint)(self.link, buffer, 1024, None, 4)
        elif name == "icon":
            self._method(self.link, index, ctypes.c_wchar_p, self.int, ctypes.POINTER(self.int))(self.link, buffer, 1024, ctypes.byref(self.int()))
        else:
            self._method(self.link, index, ctypes.c_wchar_p, self.int)(self.link, buffer, 1024)
        return buffer.value

    def save(self, path: Path) -> None:
        self._method(self.file, 6, self.text, self.int)(self.file, str(path), 1)

    def load(self, path: Path) -> "_ShellLink":
        self._method(self.file, 5, self.text, ctypes.c_uint)(self.file, str(path), 0)
        return self

    def close(self) -> None:
        for obj in (self.file, self.link):
            self._method(obj, 2)(obj)


def read_shortcut(link: Path) -> dict:
    """What a shortcut points at: path, arguments, folder, icon."""
    shell = _ShellLink().load(link)
    try:
        return {name: shell.get(name) for name in ("path", "arguments", "folder", "icon")}
    finally:
        shell.close()


def install_shortcut(data: Path, folder: Path | None = None, arguments: str = "-m app") -> Path:
    """"insta-roller" in the Start menu (or in `folder`): pythonw -m app, started in the repository."""
    link = Path(folder or start_menu()) / SHORTCUT
    link.parent.mkdir(parents=True, exist_ok=True)
    icon = write_icon(Path(data) / "insta-roller.ico")
    shell = _ShellLink()
    try:
        for name, value in (("path", pythonw_exe()), ("arguments", arguments), ("folder", str(REPO)), ("icon", str(icon)),
                            ("description", "insta-roller: filmy z nagrań 360°")):
            shell.set(name, value)
        shell.save(link)
    finally:
        shell.close()
    return link

def uninstall_shortcut(folder: Path | None = None) -> bool:
    link = Path(folder or start_menu()) / SHORTCUT
    if link.exists():
        link.unlink()
        return True
    return False


# --- the window itself ---------------------------------------------------------------------------------
def run_window(url: str, data: Path, queue, on_ready=lambda show: None) -> None:
    """Open the window on `url` and stay until the app is to quit (the window was closed with nothing
    running, the user chose to stop, or "Zakończ" in the tray). `on_ready(show)` receives the function
    that brings the window to the front: a second start of the app calls it."""
    import webview

    screens = []
    try:
        screens = [(s.x, s.y, s.width, s.height) for s in webview.screens]
    except Exception:
        pass
    place = load_geometry(data, screens)
    api = Api()
    window = webview.create_window(TITLE, url, width=place["width"], height=place["height"], x=place["x"], y=place["y"],
                                   min_size=MIN_SIZE, js_api=api, background_color="#111111", text_select=True)
    api._window = window
    quitting = threading.Event()
    tray = {"icon": None}

    def busy() -> bool:
        return any(job["status"] in ("queued", "running") for job in queue.list())

    def show(*_) -> None:
        try:
            window.show()
            window.restore()
        except Exception:
            traceback.print_exc()

    def quit_app(*_) -> None:
        quitting.set()
        try:
            window.destroy()
        except Exception:
            traceback.print_exc()

    def remember() -> None:
        try:
            save_geometry(data, int(window.width), int(window.height), int(window.x), int(window.y))
        except Exception:
            pass

    def closing():
        if quitting.is_set():
            return True
        remember()
        decision = close_decision(busy())
        if decision == "quit":
            quitting.set()
            return True
        if decision == "hide":                          # after this handler: the window's own thread waits for it
            threading.Timer(0.1, window.hide).start()
        return False                                    # the window stays (hidden or not)

    window.events.closing += closing

    def start_tray() -> None:
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem("Pokaż okno", show, default=True),
                pystray.MenuItem(lambda item: activity_text(queue.list()), None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Zakończ", quit_app))
            tray["icon"] = pystray.Icon(TITLE, mark_image(64), TITLE, menu)
            tray["icon"].run()                          # its own message loop, in this thread
        except Exception:                               # no tray: the window still works
            traceback.print_exc()

    threading.Thread(target=start_tray, name="tray", daemon=True).start()
    on_ready(show)
    try:
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(Path(data) / "webview"))
    finally:
        if tray["icon"] is not None:
            try:
                tray["icon"].stop()
            except Exception:
                pass
