"""Job steps as CLI commands. The runner executes them; the UI shows them.

Everything a job does is one of these commands, so it can be repeated by hand.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

OUT_DIR = "insta-roller"                # the CLI's default output folder inside the job folder
JOB_PROFILE = "job_profile.yaml"        # a job's own settings (profile "inne"), kept in the output folder
MODULES = {"thumbnails": ["app.thumbs"], "preview": ["app.preview"]}        # every other step is `core.cli <step>`


def python_exe() -> str:
    """The interpreter for the steps. The windowed app runs under pythonw, which has no output to read:
    the steps use the python.exe next to it."""
    here = Path(sys.executable)
    console = here.with_name("python.exe")
    return str(console) if here.name.lower() == "pythonw.exe" and console.exists() else str(here)


def pipeline(options: dict) -> tuple[list[str], str]:
    """Steps of a fresh job and the state it ends in."""
    if options.get("review"):
        return ["analyze", "plan", "thumbnails"], "review"
    return ["analyze", *render_steps(options)], "done"


def render_steps(options: dict) -> list[str]:
    """Steps after the scenes are known: a reviewed job, or a new version with another length."""
    films = options.get("films", True)          # False = "same ujęcia": only the separate shots
    return ["plan"] + (["render"] if films else []) + (["shots"] if options.get("shots") or not films else [])


def out_dir(folder: str, options: dict) -> str:
    """Where a job's results go: its own "folder wyników", else <job folder>/insta-roller."""
    return options.get("out") or os.path.join(folder, OUT_DIR)


def job_work(work: str | None, job_id: int) -> str | None:
    """A job's own folder inside the cache folder from the settings; None = the CLI's default."""
    return os.path.join(work, f"job_{job_id}") if work else None


def cli_args(step: str, folder: str, options: dict, work: str | None = None) -> list[str]:
    """Arguments after `python -m` for one step. `folder` is the job folder, an absolute path."""
    args = [*MODULES.get(step, ["core.cli", step]), folder]
    if options.get("out"):
        args.append(f"--out={options['out']}")
    if options.get("profile_yaml"):
        args.append("--profile=" + os.path.join(out_dir(folder, options), JOB_PROFILE))
    else:
        args.append(f"--profile={options.get('profile') or 'auto'}")
    for key in ("mount", "length", "source"):
        if options.get(key) and (key, options[key]) != ("source", "auto"):
            args.append(f"--{key}={options[key]:g}" if key == "length" else f"--{key}={options[key]}")
    for key in ("enable", "disable", "people"):
        if options.get(key):
            args.append(f"--{key}={','.join(options[key])}")
    if options.get("mode", "day") != "day":
        args.append(f"--mode={options['mode']}")
    if options.get("counts"):
        args.append("--counts=" + ",".join(f"{name}={int(n)}" for name, n in sorted(options["counts"].items())))
    if options.get("lengths"):
        args.append("--lengths=" + ",".join(f"{name}={seconds:g}" for name, seconds in sorted(options["lengths"].items())))
    if options.get("ai_framing"):
        args.append(f"--ai-framing={options['ai_framing']}")
    if options.get("ai_model"):
        args.append(f"--ai-model={options['ai_model']}")
    if options.get("ai_reasoning") is not None:
        args.append(f"--ai-reasoning={int(options['ai_reasoning'])}")
    if options.get("overlay") is not None:
        args.append("--overlay" if options["overlay"] else "--no-overlay")
    if work:
        args.append(f"--work={work}")
    return args
