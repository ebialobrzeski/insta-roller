"""Scene thumbnails for the review screen, cut from the proxy.

    python -m app.thumbs <folder> [the same options as `python -m core.cli analyze`]

Writes <out>/review/<scene_id>.jpg, one flat frame from the middle of every scene in
the scene type's first framing preset. The safe zone is not burnt in: the UI draws it.
"""
from __future__ import annotations

import sys


def thumbnails(job) -> None:
    from core import contracts, render
    from core.contracts import Scenes
    from core.framing import frame_shot
    from core.reel import resolve_framing

    scenes = contracts.read(job.path("scenes.json"), Scenes)
    profile, mount = job.profile, job.mount
    size = (profile.output.width // 6 * 2, profile.output.height // 6 * 2)
    recordings = {r.id: r for r in job.recordings}
    main = job.recording if profile.kind == "flight" else job.recordings[0]
    levels = {}
    if profile.kind != "flight":
        try:
            from core import holiday
            levels = holiday.levels(job, scenes) or {}
        except Exception as error:                      # thumbnails are still useful unlevelled
            print(f"thumbnails: no horizon levels ({error})")

    folder = job.path("review")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = folder / "fingerprint.txt"                  # scene ids repeat between analyses
    if not stamp.exists() or stamp.read_text() != scenes.fingerprint or job.args.force:
        for old in folder.glob("*.jpg"):
            old.unlink()
    stamp.write_text(scenes.fingerprint)

    failed = 0
    for i, scene in enumerate(scenes.scenes, 1):
        out = folder / f"{scene.id}.jpg"
        if not out.exists():
            try:
                names = profile.scene_types[scene.type].framing if scene.type in profile.scene_types else []
                try:
                    preset = resolve_framing(names, scene, mount)[0]
                except (KeyError, IndexError):
                    preset = next(iter(mount.presets))
                view = frame_shot([preset], 1.0, mount, 0.0, profile.safe_zone, size, scene.attrs)[0]
                rec = recordings.get(scene.recording, main)
                render.still(rec, mount, (scene.start_s + scene.end_s) / 2, view, size, out, job.work, job.log,
                             level=levels.get(rec.id))
            except Exception as error:
                failed += 1
                print(f"thumbnails: {scene.id}: {error}")
        print(f"  shot {i}/{len(scenes.scenes)}  {scene.id}", flush=True)     # same progress format as render
    print(f"thumbnails: {len(scenes.scenes) - failed} of {len(scenes.scenes)} -> {folder}")
    if failed and failed == len(scenes.scenes):
        sys.exit("thumbnails: none could be made")


def main(argv: list[str] | None = None) -> None:
    from core import cli
    # Borrow the CLI's own job options: with the step swapped, `analyze <folder> ...` makes thumbnails.
    cli.step_analyze = thumbnails
    cli.main(["analyze", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    main()
