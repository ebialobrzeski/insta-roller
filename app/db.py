"""The job queue in SQLite. One row per job folder."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder TEXT UNIQUE NOT NULL COLLATE NOCASE,     -- absolute path of the job folder
    options TEXT NOT NULL,                  -- JSON, see app.web.JobOptions
    steps TEXT NOT NULL,                    -- JSON list of the current run's steps
    step_index INTEGER NOT NULL DEFAULT 0,  -- first step that is not finished
    final TEXT NOT NULL DEFAULT 'done',     -- state after the last step: done | review
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | running | review | done | error | cancelled
    progress REAL NOT NULL DEFAULT 0,
    log TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    cancel INTEGER NOT NULL DEFAULT 0,
    queued REAL NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL
)"""
LOG_LINES = 300
ACTIVE = ("queued", "running")


class Busy(Exception):
    """The job is queued or running, so it cannot be changed now."""


class Queue:
    def __init__(self, path: Path, clock=time.time):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)
        self.lock = threading.Lock()
        self.clock = clock

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def _job(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        job = dict(row)
        job["options"], job["steps"] = json.loads(job["options"]), json.loads(job["steps"])
        job["stage"] = job["steps"][job["step_index"]] if job["step_index"] < len(job["steps"]) else ""
        return job

    def _set(self, job_id: int, **values) -> None:
        values["updated"] = self.clock()
        self.db.execute(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
                        [*values.values(), job_id])

    def _get(self, job_id: int) -> dict | None:
        return self._job(self.db.execute("SELECT * FROM jobs WHERE id = ?", [job_id]).fetchone())

    # --- UI side ----------------------------------------------------------
    def get(self, job_id: int) -> dict | None:
        with self.lock:
            return self._get(job_id)

    def find(self, folder: str) -> dict | None:
        with self.lock:
            return self._job(self.db.execute("SELECT * FROM jobs WHERE folder = ?", [folder]).fetchone())

    def list(self) -> list[dict]:
        with self.lock:
            return [self._job(r) for r in self.db.execute("SELECT * FROM jobs ORDER BY queued DESC, id DESC")]

    def submit(self, folder: str, options: dict, steps: list[str], final: str) -> tuple[dict, bool]:
        """Queue a folder. A folder that has a job keeps its row: (job, True)."""
        with self.lock:
            now = self.clock()
            row = self.db.execute("SELECT * FROM jobs WHERE folder = ?", [folder]).fetchone()
            if row is None:
                cur = self.db.execute(
                    "INSERT INTO jobs (folder, options, steps, final, queued, created, updated) VALUES (?,?,?,?,?,?,?)",
                    [folder, json.dumps(options), json.dumps(steps), final, now, now, now])
                return self._get(cur.lastrowid), False
            if row["status"] != "running":
                self._start(row["id"], steps, final, options)
            return self._get(row["id"]), True

    def _start(self, job_id: int, steps: list[str], final: str, options: dict | None = None) -> None:
        values = dict(steps=json.dumps(steps), final=final, step_index=0, status="queued", progress=0.0,
                      error="", cancel=0, queued=self.clock())
        if options is not None:
            values["options"] = json.dumps(options)
        self._set(job_id, **values)

    def restart(self, job_id: int, steps: list[str], final: str, options: dict | None = None) -> dict:
        """Queue another run of an existing job (render after the review, a new version)."""
        with self.lock:
            job = self._get(job_id)
            if job["status"] in ACTIVE:
                raise Busy
            self._start(job_id, steps, final, options)
            return self._get(job_id)

    def retry(self, job_id: int) -> dict:
        """Back to the queue from the step that failed; the finished steps are cached anyway."""
        with self.lock:
            job = self._get(job_id)
            if job["status"] in ACTIVE:
                raise Busy
            index = job["step_index"] if job["step_index"] < len(job["steps"]) else 0
            self._set(job_id, status="queued", step_index=index, error="", cancel=0, queued=self.clock())
            return self._get(job_id)

    def cancel(self, job_id: int) -> dict:
        with self.lock:
            job = self._get(job_id)
            if job["status"] == "running":
                self._set(job_id, cancel=1)             # the runner stops the step at its next report
            elif job["status"] in ("queued", "review"):
                self._set(job_id, status="cancelled")
            return self._get(job_id)

    def delete(self, job_id: int) -> None:
        """Forget a job. Only the queue row goes: its folders and films are not touched."""
        with self.lock:
            job = self._get(job_id)
            if job["status"] in ACTIVE:
                raise Busy
            self.db.execute("DELETE FROM jobs WHERE id = ?", [job_id])

    # --- runner side ------------------------------------------------------
    def recover(self) -> None:
        """A job that was running when the app stopped goes back to the queue, at the step it was in."""
        with self.lock:
            for row in self.db.execute("SELECT id, cancel FROM jobs WHERE status = 'running'").fetchall():
                self._set(row["id"], status="cancelled" if row["cancel"] else "queued", cancel=0)

    def claim(self) -> dict | None:
        """The job waiting longest becomes the running one."""
        with self.lock:
            row = self.db.execute("SELECT id FROM jobs WHERE status = 'queued' ORDER BY queued, id LIMIT 1").fetchone()
            if row is None:
                return None
            self._set(row["id"], status="running")
            return self._get(row["id"])

    def report(self, job_id: int, step_index: int, progress: float, lines: list[str]) -> dict | None:
        """Progress and new log lines of the running job. None when it is not running any more."""
        with self.lock:
            job = self._get(job_id)
            if job is None or job["status"] != "running":
                return None
            log = (job["log"].splitlines() + lines)[-LOG_LINES:]
            self._set(job_id, step_index=step_index, progress=progress, log="\n".join(log))
            return {"cancel": bool(job["cancel"])}

    def finish(self, job_id: int, status: str, error: str = "") -> dict | None:
        with self.lock:
            job = self._get(job_id)
            if job is None or job["status"] != "running":
                return None
            if status == "done":
                self._set(job_id, status=job["final"], step_index=len(job["steps"]), progress=1.0, cancel=0)
            else:
                self._set(job_id, status=status, error=error, cancel=0)
            return self._get(job_id)
