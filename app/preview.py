"""Previews for the review screen: what a shot will look like, before it is rendered.

    python -m app.preview <folder> [the same options as `python -m core.cli analyze`]

Reads a list of wanted pictures from the JSON file named in INSTA_ROLLER_PREVIEW and
writes them below <out>/review/, always from the low-resolution proxy so it is quick:

  stills: one frame of a moment seen through one view (a preset of the mount, or a
          view an AI made for the shot)
  clips:  a whole shot with its camera move, as a small video
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
from pathlib import Path

SPEC_ENV = "INSTA_ROLLER_PREVIEW"
CLIP_WIDTH = 360                    # clips are small: they only have to show the move


def previews(job) -> None:
    from core import contracts, render
    from core.contracts import ReelPlan, Scenes, Shot, Telemetry
    from core.framing import frame_shot, plan_framing, shot_mount

    spec = json.loads(Path(os.environ[SPEC_ENV]).read_text(encoding="utf-8"))
    scenes = contracts.read(job.path("scenes.json"), Scenes)
    profile = job.profile
    job.args.source = "proxy"
    ctx = job.context(scenes)
    ctx = dataclasses.replace(ctx, stitcher=None, source="proxy")
    folder = job.path("review")
    out = profile.output
    size = (out.width // 6 * 2, out.height // 6 * 2)
    wanted = spec.get("stills", []) + spec.get("clips", [])
    done = 0

    def shot_of(item: dict) -> Shot:
        return Shot.model_validate({"index": 1, "type": "", "framing": [], **item["shot"]})

    for item in spec.get("stills", []):
        target = folder / item["file"]
        try:
            shot = shot_of(item)
            mount = shot_mount(job.mount, shot)
            preset = item["preset"]
            view = frame_shot([preset], 1.0, mount, 0.0, profile.safe_zone, size, shot.attrs, item["at"])[0]
            render.still(ctx.recording(shot.recording), mount, item["at"], view, size, target, job.work, job.log,
                         level=(ctx.levels or {}).get(shot.scene_id), projection=mount.presets[preset].projection)
        except Exception as error:                      # one bad view must not hide the others
            print(f"preview: {item['file']}: {error}")
        done += 1
        print(f"  shot {done}/{len(wanted)}  still", flush=True)

    if spec.get("clips"):
        scale = CLIP_WIDTH / out.width
        small = profile.model_copy(deep=True)
        small.output.width, small.output.height = CLIP_WIDTH, int(out.height * scale) // 2 * 2
        small.audio = False
        tele_path = job.path("telemetry.json")
        tele = contracts.read(tele_path, Telemetry) if tele_path.exists() else None
        for item in spec["clips"]:
            target = folder / item["file"]
            try:
                shot = shot_of(item)
                reel = ReelPlan(theme="preview", target_length_s=shot.duration_s, length_s=shot.duration_s, shots=[shot])
                calibration = ctx.recording(shot.recording).calibration(job.mount)
                framing = plan_framing(reel, small, job.mount, tele, calibration)
                made = render.render_shot(framing.shots[0], framing, dataclasses.replace(ctx, profile=small))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(made, target)
            except Exception as error:
                print(f"preview: {item['file']}: {error}")
            done += 1
            print(f"  shot {done}/{len(wanted)}  clip", flush=True)
    print(f"preview: {done} -> {folder}")


def main(argv: list[str] | None = None) -> None:
    from core import cli
    # Borrow the CLI's own job options, as app.thumbs does.
    cli.step_analyze = previews
    cli.main(["analyze", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    main()
