"""The "Osoby" page: who is in people.json, and refreshing it from Immich.

The refresh is the ordinary CLI command `python -m core.cli faces-export`, run in the background.
The Immich address and API key come from the environment or the repository's .env file. The key
reaches the command only through its environment; the page is told only whether it is set.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import os

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

ADDRESSES = ("IMMICH_TAILSCALE_ADDRESS", "IMMICH_ADDRESS")      # tried in this order
KEY = "IMMICH_API_KEY"
LOG_LINES = 40


class Refresh(BaseModel):
    names: list[str]


def read_env(path: Path) -> dict[str, str]:
    """KEY=value lines of a .env file."""
    values = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        name, sep, value = line.strip().partition("=")
        if sep and not name.startswith("#"):
            values[name.removeprefix("export ").strip()] = value.strip().strip("\"'")
    return values


def with_scheme(address: str) -> str:
    address = address.strip().rstrip("/")
    return address if "://" in address else f"http://{address}"


def reachable(url: str, timeout_s: float = 3.0) -> bool:
    """Whether something listens at the address (a NAS behind Tailscale may be away)."""
    parts = urlsplit(url)
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        with socket.create_connection((parts.hostname, port), timeout=timeout_s):
            return True
    except (OSError, ValueError, TypeError):
        return False


def add_routes(app: FastAPI, people_file: Path, env_file: Path, profiles: Path, runner, settings, save) -> None:
    state = {"status": "idle", "log": [], "error": "", "finished": 0.0}     # the one refresh, in memory
    lock = threading.Lock()

    def immich() -> dict[str, str]:
        values = read_env(env_file)
        values.update({k: os.environ[k] for k in (*ADDRESSES, KEY) if os.environ.get(k)})
        return {k: values.get(k, "").strip() for k in (*ADDRESSES, KEY)}

    def default_names() -> list[str]:
        try:
            return list(yaml.safe_load((profiles / "holiday.yaml").read_text("utf-8")).get("people") or {})
        except Exception:
            return []

    def hide(text: str, conf: dict[str, str]) -> str:
        """Log lines and commands as the page may show them: no key, no addresses."""
        for name, value in sorted(conf.items(), key=lambda item: -len(item[1])):
            if value:
                text = text.replace(with_scheme(value), f"%{name}%").replace(value, f"%{name}%")
        return text

    @app.get("/api/people")
    def people():
        try:
            data = json.loads(people_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        listed = []
        for person in (data.get("people") if isinstance(data, dict) else None) or []:
            if isinstance(person, dict) and person.get("name"):
                listed.append({"name": str(person["name"]), "samples": len(person.get("embeddings") or [])})
        conf = immich()
        return {"present": data is not None, "people": listed,
                "exported_at": data.get("exported_at", "") if isinstance(data, dict) else "",
                "names": settings()["people_names"] or default_names(),
                "env": {name: bool(value) for name, value in conf.items()},
                "refresh": {**state, "log": state["log"][-LOG_LINES:]}}

    def work(names: list[str], conf: dict[str, str]) -> None:
        def say(line: str) -> None:
            state["log"].append(hide(line, conf))

        try:
            urls = [with_scheme(conf[name]) for name in ADDRESSES if conf[name]]
            url = next((u for u in urls[:-1] if reachable(u)), urls[-1])        # the last one is not asked first
            cmd = [sys.executable, "-u", "-m", "core.cli", "faces-export", "--people", ",".join(names),
                   "--out", str(people_file), "--immich-url", url, "--api-key-env", KEY]
            say("$ " + subprocess.list2cmdline(cmd))
            code = runner(cmd, say, lambda: False, {KEY: conf[KEY]})
            last = next((line for line in reversed(state["log"]) if line.strip()), "")
            state.update(status="done" if code == 0 else "error", error="" if code == 0 else last)
        except Exception as error:
            state.update(status="error", error=hide(str(error), conf))
        state["finished"] = time.time()

    @app.post("/api/people/refresh")
    def refresh(body: Refresh):
        """Run `faces-export` for these names in the background; GET /api/people shows how it goes."""
        names = [n.strip() for n in body.names if n.strip()]
        if not names or any("," in n or n.startswith("-") for n in names):
            raise HTTPException(422, "Podaj imiona tak, jak są zapisane w Immichu (bez przecinków)")
        conf = immich()
        if not conf[KEY]:
            raise HTTPException(409, f"Brak klucza Immicha: ustaw zmienną {KEY} (w środowisku albo w pliku .env)")
        if not any(conf[name] for name in ADDRESSES):
            raise HTTPException(409, f"Brak adresu Immicha: ustaw {ADDRESSES[0]} albo {ADDRESSES[1]}")
        with lock:
            if state["status"] == "running":
                raise HTTPException(409, "Odświeżanie już trwa")
            state.update(status="running", log=[], error="")
        save({**settings(), "people_names": names})
        threading.Thread(target=work, args=(names, conf), name="people-refresh", daemon=True).start()
        return {"status": "running"}
