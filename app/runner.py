"""The job runner: a thread that takes queued jobs and runs their steps as CLI subprocesses."""
from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .db import Queue
from .steps import cli_args, job_work, out_dir, python_exe

REPO = Path(__file__).resolve().parent.parent
SHOT = re.compile(r"shot (\d+)/(\d+)")
_job_object = None


def _tie_to_app(proc: subprocess.Popen) -> None:
    """Windows: put the step in a job object that closes with this process, so the CLI and its
    FFmpeg children never outlive the app, even when the app is killed."""
    global _job_object
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("Basic", Basic), ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    try:
        k32 = ctypes.WinDLL("kernel32")
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        if _job_object is None:
            info = Extended()
            info.Basic.LimitFlags = 0x2000              # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            handle = k32.CreateJobObjectW(None, None)
            if not handle or not k32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                return
            _job_object = handle                        # kept open for the life of the app
        k32.AssignProcessToJobObject(_job_object, int(proc._handle))
    except (OSError, AttributeError):
        pass                                            # stopping a step still kills its tree, see below


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop a step together with the FFmpeg processes it started."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    proc.wait()


def run_subprocess(cmd: list[str], on_line, poll, env: dict | None = None) -> int:
    """Run one step. on_line(text) gets every output line; poll() is called at least once a
    second and returns True when the step must be stopped (cancelled, or the app is closing).
    `env` adds variables for the step: secrets go there, never on the command line."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", **(env or {}))
    # Its own process group: Ctrl+C in the app's console does not reach the step; the app stops it.
    # No console window of its own either: the windowed app has none to share.
    own = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt"
           else {"start_new_session": True})
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            cwd=REPO, env=env, text=True, encoding="utf-8", errors="replace", **own)
    if os.name == "nt":
        _tie_to_app(proc)
    lines: queue.Queue = queue.Queue()

    def pump():
        for line in proc.stdout:
            lines.put(line.rstrip())
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    try:
        while True:
            try:
                line = lines.get(timeout=1.0)
            except queue.Empty:
                line = ""
            if line is None:
                return proc.wait()
            if line:
                on_line(line)
            if poll():
                kill_tree(proc)
                return -1
    except BaseException:
        kill_tree(proc)
        raise


class Runner:
    """Runs the queue, one job at a time. `work` returns the cache folder from the settings."""

    def __init__(self, jobs: Queue, runner=run_subprocess, work=lambda: "", interval_s: float = 1.0,
                 python: str | None = None):
        self.jobs, self.runner, self.work = jobs, runner, work
        self.interval_s, self.python = interval_s, python or python_exe()
        self.closing = threading.Event()
        self.thread: threading.Thread | None = None
        self._guard = threading.Lock()

    def _say(self, line: str) -> None:
        with self._guard:
            self._lines.append(line)

    def _alongside(self, step: str, folder: str, options: dict, work: str | None) -> dict:
        """Start `step` in a thread of its own, beside the step that runs now. Until _await() makes
        it the step that runs now, its progress lines are left out of the log: the bar and the
        activity line belong to the main step."""
        cmd = [self.python, "-u", "-m", *cli_args(step, folder, options, work)]
        side = {"code": None, "error": "", "quit": threading.Event(), "cmd": cmd, "main": None, "text": ""}

        def on_line(line: str) -> None:
            side["text"] = line.strip() or side["text"]
            if side["main"] is not None:
                side["main"](line)
            elif not SHOT.search(line):
                self._say(line)

        def run() -> None:
            try:
                side["code"] = self.runner(cmd, on_line, lambda: side["quit"].is_set() or self._stop
                                           or self.closing.is_set())
            except Exception as error:
                side["code"], side["error"] = -1, str(error)
        self._say(f"{step}: started alongside")
        side["thread"] = threading.Thread(target=run, name=f"step-{step}", daemon=True)
        side["thread"].start()
        return side

    def _await(self, side: dict, on_line) -> int:
        """Wait for a step started by _alongside(); from now on it is the step that runs now."""
        with self._guard:
            side["main"] = on_line
            self._lines.append("$ " + subprocess.list2cmdline(side["cmd"]))
        while side["thread"].is_alive():
            side["thread"].join(0.5)
            self._report()
        return side["code"] if side["code"] is not None else -1

    @staticmethod
    def _drop(side: dict | None) -> None:
        """Stop a step running alongside and wait for it to end."""
        if side is not None:
            side["quit"].set()
            side["thread"].join()

    # --- talking to the queue ------------------------------------------------
    def _report(self, force: bool = False) -> bool:
        """Store the progress and the new log lines. True = stop the step."""
        if self.closing.is_set():
            return True
        if not force and time.monotonic() - self._sent < self.interval_s:
            return self._stop
        self._sent = time.monotonic()
        with self._guard:                           # a step running alongside adds lines from its own thread
            lines, self._lines = self._lines, []
        answer = self.jobs.report(self._job, self._index, round(self._progress, 3), lines)
        if answer is None or answer["cancel"]:
            self._stop = True
        return self._stop

    # --- running jobs --------------------------------------------------------
    @staticmethod
    def _reels(out: str) -> int:
        try:
            reels = json.loads((Path(out) / "reels.json").read_text(encoding="utf-8"))["reels"]
            return max(sum(r.get("status") == "planned" for r in reels), 1)
        except (OSError, ValueError, KeyError, TypeError):
            return 1

    def run_job(self, job: dict) -> None:
        self._job, self._lines, self._sent, self._stop = job["id"], [], 0.0, False
        self._index, self._progress = job.get("step_index", 0), job.get("progress", 0.0)
        steps, folder, options = job["steps"], job["folder"], job["options"]
        work = job_work(self.work(), job["id"])
        try:
            self._steps(job, steps, folder, options, work)
        finally:
            self._drop(self._side)                  # e.g. the runner itself failed

    def _steps(self, job: dict, steps: list[str], folder: str, options: dict, work: str | None) -> None:
        self._side = None                           # the thumbnails, made while the plan waits for the model
        for index in range(job.get("step_index", 0), len(steps)):
            step = steps[index]
            cmd = [self.python, "-u", "-m", *cli_args(step, folder, options, work)]
            self._index, self._progress = index, index / len(steps)
            if not (step == "thumbnails" and self._side is not None):
                self._say("$ " + subprocess.list2cmdline(cmd))
            reels = self._reels(out_dir(folder, options)) if step in ("render", "shots") else 1
            seen = {"last": 0, "reel": 0, "text": ""}

            def on_line(line: str, index=index, reels=reels, seen=seen) -> None:
                self._say(line)
                seen["text"] = line.strip() or seen["text"]
                match = SHOT.search(line)
                if match:                           # "shot i/n" comes before shot i is rendered
                    i, n = int(match[1]), max(int(match[2]), 1)
                    seen["reel"] += i < seen["last"]
                    seen["last"] = i
                    part = min((seen["reel"] + (i - 1) / n) / reels, 1.0)
                    self._progress = (index + part) / len(steps)

            if self._report(force=True):
                break
            # The scene thumbnails need only what analyze found: they are made while the plan
            # mostly waits for the model's answers, not after it.
            if step == "plan" and "thumbnails" in steps[index + 1:]:
                self._side = self._alongside("thumbnails", folder, options, work)
            try:
                if step == "thumbnails" and self._side is not None:
                    code = self._await(self._side, on_line)
                    seen["text"] = self._side["error"] or self._side["text"]
                else:
                    code = self.runner(cmd, on_line, self._report)
            except Exception as error:              # e.g. the interpreter could not be started
                code, seen["text"] = -1, str(error)
            if code != 0 and not self._stop:        # Ctrl+Break reaches the step as well as the app:
                self.closing.wait(0.3)              # a step that died of it is not a failed step
            if self._stop or self.closing.is_set():
                break
            if step == "thumbnails":
                self._side = None
            if code != 0 and step == "thumbnails":  # the review works without pictures
                self._say("thumbnails: failed, continuing without them")
            elif code != 0:
                self._drop(self._side)
                self._side = None
                self._report(force=True)
                self.jobs.finish(job["id"], "error",
                                 f"{step}: exit code {code}" + (f": {seen['text']}" if seen["text"] else ""))
                return
        self._drop(self._side)                      # stopped or closing: the step alongside ends too
        self._side = None
        if self.closing.is_set():                   # stays "running": Queue.recover() resumes it at this step
            with self._guard:
                lines, self._lines = self._lines, []
            self.jobs.report(job["id"], self._index, round(self._progress, 3), lines)
            return
        self._report(force=True)
        self.jobs.finish(job["id"], "cancelled" if self._stop else "done")

    def run_once(self) -> bool:
        """Take and run one job. False when nothing is waiting."""
        job = self.jobs.claim()
        if job is None:
            return False
        print(f"job {job['id']}: {job['folder']} ({', '.join(job['steps'][job.get('step_index', 0):])})")
        self.run_job(job)
        print(f"job {job['id']}: " + ("stopped; it continues when the app starts again" if self.closing.is_set()
                                      else (self.jobs.get(job["id"]) or {}).get("status", "gone")))
        return True

    # --- the background thread -------------------------------------------------
    def run_forever(self, poll_s: float = 1.0) -> None:
        while not self.closing.is_set():
            try:
                busy = self.run_once()
            except Exception as error:              # the queue must survive one bad job
                print(f"runner: {error!r}")
                busy = False
            if not busy:
                self.closing.wait(poll_s)

    def start(self) -> None:
        """Pick up what was running when the app last stopped, then run the queue in the background."""
        self.jobs.recover()
        self.thread = threading.Thread(target=self.run_forever, name="job-runner", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Stop the running step (the whole process tree) and leave its job to be resumed."""
        self.closing.set()
        if self.thread:
            self.thread.join()
        self.jobs.recover()
