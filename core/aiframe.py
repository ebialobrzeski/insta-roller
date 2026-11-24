"""AI framing: a model looks at the possible views of every shot and frames it.

For each shot the possible views are rendered as small stills from the middle
of the shot. Two engines can judge them:

  local       CLIP (ViT-B/32, ONNX) on this PC: every still is compared with
              sentences that describe a good and a poor picture, and the best
              of the framings the themes offer is taken.
  openrouter  a vision model behind the OpenRouter API directs the reel. For
              every shot it sees the whole 360 sphere as a panorama with a
              grid of angles, plus the ready-made views, and aims the camera
              itself: any direction, field of view, zoom, pan, projection
              (one to three views in a row), with the rhythm and variety of
              the whole film in mind. Showpieces (small planet, fisheye, roll)
              and shots of the pilot / holder are rationed.
              The stills leave the PC; needs OPENROUTER_API_KEY.

The choice replaces `framing` of the shots in the reel plan. Anything going
wrong (no model, no key, no network) leaves the theme's own framing in place.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
from pathlib import Path

import numpy as np

from .config import PROFILES_DIR, Mount, Preset, Profile, Theme, load_theme
from .contracts import Keyframe, ReelPlan, Scene, Shot
from .framing import frame_shot
from .ingest import fingerprint
from .reel import resolve_framing

STILL = (384, 384)              # candidates are judged on small square-ish stills
CLIP_REPO = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main"
CLIP_FILES = {"tokenizer.json": "tokenizer.json", "text_model.onnx": "onnx/text_model.onnx",
              "vision_model.onnx": "onnx/vision_model.onnx"}
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
KEY_ENV = "OPENROUTER_API_KEY"
BATCH = 30                      # shots per request: a whole reel, so the model sees the film as one
MIN_VIEW_S = 2.5                # a view in a sequence needs at least this long
ANSWER_TOKENS = 8000           # room for the answer itself, on top of the reasoning budget
PROMPT = 6                      # raised when the instructions change, so cached answers are not reused


class Unavailable(RuntimeError):
    """The engine cannot run here (model files, key, network)."""


# --- candidates ---------------------------------------------------------------------------
def _plain(names: list[str], mount: Mount) -> bool:
    return all(mount.presets[n].projection == "flat" and not mount.presets[n].roll_turns for n in names)


def candidates(shot: Shot, scene: Scene | None, profile: Profile, theme: Theme | None, mount: Mount,
               themes: list[Theme]) -> list[list[str]]:
    """Framings this shot could take: its own, its theme's, and the ordinary
    (flat, no roll) framings other themes use for the same scene type."""
    found: list[list[str]] = [list(shot.framing)]
    if scene is None or shot.type not in profile.scene_types:
        return found

    def add(options, own: bool) -> None:
        for names in options:
            try:
                resolved = resolve_framing(names, scene, mount)
            except KeyError:
                continue
            projections = {mount.presets[n].projection for n in resolved}
            if len(projections) == 1 and (own or _plain(resolved, mount)) and resolved not in found:
                found.append(resolved)

    if theme is not None:
        add(theme.variants.get(shot.type, []), True)
        add([theme.framing[shot.type]] if shot.type in theme.framing else [], True)
    add([profile.scene_types[shot.type].framing], False)
    for other in themes:
        add(other.variants.get(shot.type, []), False)
        add([other.framing[shot.type]] if shot.type in other.framing else [], False)
    return found


def views(shot: Shot, scene: Scene | None, profile: Profile, theme: Theme | None, mount: Mount,
          themes: list[Theme]) -> tuple[list[str], set[str]]:
    """Views a director may use for this shot, and which of them are accents:
    the unusual views (planet, fisheye, roll) that belong to another theme."""
    names: list[str] = []
    for option in candidates(shot, scene, profile, theme, mount, themes):
        names += [n for n in option if n not in names]
    accents: set[str] = set()
    if scene is not None and profile.ai_framing.accents > 0:
        for other in themes:
            for option in other.variants.get(shot.type, []):
                try:
                    resolved = resolve_framing(option, scene, mount)
                except KeyError:
                    continue
                for n in resolved:
                    if n not in names and not _plain([n], mount):
                        names.append(n)
                        accents.add(n)
    return names, accents


def describe(name: str, mount: Mount, kind: str) -> str:
    """What a view shows and how its camera moves, in words a model can use."""
    p = mount.presets[name]
    if p.track:
        where = {"travel": "the way ahead", "holder": "the person holding the camera",
                 "person": "a companion who was recognised"}.get(p.track, p.track)
        side = "" if abs(p.yaw) < 20 else f", turned {abs(p.yaw):.0f} deg to the {'right' if p.yaw > 0 else 'left'}"
        looks = f"towards {where}{side}"
    else:
        yaw = (p.yaw + 180) % 360 - 180
        looks = ("ahead" if abs(yaw) < 30 else "backwards" + (" at the pilot" if kind == "flight" else "") if abs(yaw) > 150
                 else f"to the {'right' if yaw > 0 else 'left'}" + (" and backwards" if abs(yaw) > 105 else ""))
    if p.pitch <= -70:
        looks = "straight down"
    elif p.pitch <= -25:
        looks += ", down at the ground"
    parts = [f"looks {looks}"]
    if p.projection == "planet":
        parts.append("small-planet projection: the surroundings wrap into a ball" if p.fov > 200
                     else "wide view in the same projection as the small planet, so a planet view can unroll into it")
    elif p.projection == "fisheye":
        parts.append("round ultra-wide fisheye")
    else:
        parts.append("close" if p.fov < 95 else "wide" if p.fov > 122 else "normal lens")
    if p.fov_end is not None:
        parts.append("slowly zooms in" if p.fov_end < p.fov else "slowly zooms out, opening the view")
    if p.pan_dps:
        parts.append("slowly pans round")
    if p.roll_turns:
        parts.append("the picture rolls once all the way over")
    if p.horizon == "locked":
        parts.append("horizon held level")
    return "; ".join(parts)


FOV = {"flat": (60.0, 140.0), "planet": (140.0, 320.0), "fisheye": (160.0, 230.0)}
PROJECTION = {"normal": "flat", "flat": "flat", "planet": "planet", "fisheye": "fisheye"}


def custom_preset(spec: dict, profile: Profile, hold_s: float) -> Preset:
    """A view the model aimed itself, with every number brought into a sane range.
    hold_s: how long the view is held (source seconds), to turn a pan angle into a rate."""
    projection = PROJECTION[str(spec.get("projection", "normal")).lower()]
    low, high = FOV[projection]

    def fov(value) -> float:
        return round(min(max(float(value), low), high), 1)
    wide = fov(spec.get("fov", 110 if projection == "flat" else high))
    zoom = spec.get("zoom_to")
    zoom = None if zoom in (None, "", 0) or abs(fov(zoom) - wide) < 3 else fov(zoom)
    limit = profile.max_turn_dps
    pan = float(spec.get("pan") or 0.0) / max(hold_s, 0.5)
    level = projection == "flat" and str(spec.get("horizon", "")).lower() == "level"
    return Preset(yaw=round((float(spec["yaw"]) + 180) % 360 - 180, 1),
                  pitch=round(min(max(float(spec.get("pitch", 0.0)), -90.0), 90.0), 1),
                  fov=wide, fov_end=zoom, projection=projection, pan_dps=round(min(max(pan, -limit), limit), 2),
                  roll_turns=1.0 if spec.get("roll") else 0.0, horizon="locked" if level else "aircraft",
                  safe_zone=projection == "flat" and profile.kind == "flight")


def shows_person(preset: Preset, mount: Mount) -> bool:
    """Flight: the view has the pilot in it (it looks about where the `pilot` view looks)."""
    name = mount.aliases.get("pilot", "pilot")
    if name not in mount.presets or preset.track:
        return preset.track == "holder"
    pilot = mount.presets[name]
    off = abs((preset.yaw - pilot.yaw + 180) % 360 - 180)
    return preset.projection == "flat" and off < 50 and preset.pitch > -45 and not preset.pan_dps


def max_views(shot: Shot) -> int:
    return max(1, min(3, int(shot.duration_s / shot.speed / MIN_VIEW_S)))


def valid_sequence(names: list[str], shot: Shot, presets: dict[str, Preset]) -> bool:
    """One to three known views in one projection, with time for each."""
    if not names or len(names) > 3 or any(n not in presets for n in names):
        return False
    if len({presets[n].projection for n in names}) > 1:
        return False
    return len(names) == 1 or shot.duration_s / shot.speed / len(names) >= MIN_VIEW_S


# --- stills -------------------------------------------------------------------------------
def _stills(shot: Shot, presets: list[str], ctx, folder: Path) -> dict[str, Path]:
    from . import render
    rec = ctx.recording(shot.recording)
    at = shot.start_s + shot.duration_s / 2
    level = (ctx.levels or {}).get(shot.scene_id)
    out = {}
    for name in presets:
        path = folder / f"{fingerprint(rec.id, round(at, 2), name, json.dumps(shot.attrs, sort_keys=True), ctx.mount.presets[name].model_dump_json(), level)}.jpg"
        if not path.exists():
            view = frame_shot([name], 1.0, ctx.mount, 0.0, None, STILL, shot.attrs, at)[0]
            render.still(rec, ctx.mount, at, view, STILL, path, ctx.work, ctx.log, level=level,
                         projection=ctx.mount.presets[name].projection)
        out[name] = path
    return out


def _own_stills(shot: Shot, own: dict, ctx, folder: Path) -> dict[str, Path]:
    """What the views the model aimed itself actually show."""
    import dataclasses
    mount = ctx.mount.model_copy(update={"presets": {**ctx.mount.presets, **{n: Preset.model_validate(v) for n, v in own.items()}}})
    return _stills(shot, list(own), dataclasses.replace(ctx, mount=mount), folder)


PANO = (1024, 512)


def _pano(shot: Shot, ctx, folder: Path) -> Path:
    """The whole sphere at the middle of the shot, in the frame the views are aimed in."""
    from . import render
    rec = ctx.recording(shot.recording)
    at = shot.start_s + shot.duration_s / 2
    level = (ctx.levels or {}).get(shot.scene_id)
    path = folder / f"pano_{fingerprint(rec.id, round(at, 2), level, ctx.mount.model_dump_json(include={'calibration'}))}.jpg"
    if not path.exists():
        view = Keyframe(t=0, yaw=0, pitch=0, roll=0, fov=100)
        render.still(rec, ctx.mount, at, view, PANO, path, ctx.work, ctx.log, level=level, projection="pano")
    return path


def pano_sheet(path: Path) -> bytes:
    """The panorama with a grid: yaw along the width, pitch along the height."""
    from PIL import Image, ImageDraw
    image = Image.open(path).convert("RGB").resize(PANO)
    draw = ImageDraw.Draw(image, "RGBA")
    w, h = PANO
    for yaw in range(-180, 181, 45):
        x = min(int((yaw + 180) / 360 * w), w - 1)
        draw.line([x, 0, x, h], fill=(255, 255, 255, 110), width=1)
        draw.rectangle([min(x + 2, w - 44), 2, min(x + 44, w - 2), 16], fill=(0, 0, 0, 170))
        draw.text((min(x + 4, w - 42), 3), f"yaw {yaw}", fill="white")
    for pitch in range(-60, 61, 30):
        y = int((90 - pitch) / 180 * h)
        draw.line([0, y, w, y], fill=(255, 255, 255, 110), width=1)
        draw.rectangle([2, y + 2, 58, y + 16], fill=(0, 0, 0, 170))
        draw.text((4, y + 3), f"pitch {pitch}", fill="white")
    data = io.BytesIO()
    image.save(data, "JPEG", quality=82)
    return data.getvalue()


# --- local engine: CLIP ---------------------------------------------------------------------
def model_dir() -> Path:
    root = os.environ.get("INSTA_ROLLER_MODELS")
    base = Path(root) if root else Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "insta-roller" / "models"
    return base / "clip-vit-b32"


def ensure_model(progress=print) -> Path:
    """The CLIP files, downloaded once (about 610 MB) when they are not there yet."""
    folder = model_dir()
    missing = [name for name in CLIP_FILES if not (folder / name).exists()]
    if missing:
        import urllib.request
        folder.mkdir(parents=True, exist_ok=True)
        for name in missing:
            progress(f"ai framing: downloading {name} from huggingface.co (once)")
            part = folder / f"{name}.part"
            try:
                urllib.request.urlretrieve(f"{CLIP_REPO}/{CLIP_FILES[name]}", part)
            except OSError as error:
                raise Unavailable(f"cannot download the local model ({error})") from None
            part.replace(folder / name)
    return folder


class Clip:
    def __init__(self, folder: Path):
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as error:
            raise Unavailable(f"the local model needs `pip install -e \".[ai]\"` ({error.name} is missing)") from None
        providers = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in ort.get_available_providers()]
        options = ort.SessionOptions()
        options.log_severity_level = 3
        self.vision = ort.InferenceSession(str(folder / "vision_model.onnx"), options, providers=providers)
        self.text = ort.InferenceSession(str(folder / "text_model.onnx"), options, providers=providers)
        self.tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))

    @staticmethod
    def _unit(x: np.ndarray) -> np.ndarray:
        return x / np.linalg.norm(x, axis=-1, keepdims=True)

    def texts(self, sentences: list[str]) -> np.ndarray:
        rows = []
        for sentence in sentences:
            ids = self.tokenizer.encode(sentence).ids[:77]
            feed = {"input_ids": np.array([ids], dtype=np.int64)}
            if any(i.name == "attention_mask" for i in self.text.get_inputs()):
                feed["attention_mask"] = np.ones((1, len(ids)), dtype=np.int64)
            rows.append(self.text.run(None, feed)[0][0])
        return self._unit(np.array(rows))

    def images(self, paths: list[Path]) -> np.ndarray:
        from PIL import Image
        mean = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
        std = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
        rows = []
        for path in paths:
            image = Image.open(path).convert("RGB").resize((224, 224), Image.BICUBIC)
            pixels = (np.asarray(image, dtype=np.float32) / 255.0 - mean) / std
            rows.append(self.vision.run(None, {"pixel_values": pixels.transpose(2, 0, 1)[None]})[0][0])
        return self._unit(np.array(rows))


def clip_scores(images: np.ndarray, good: np.ndarray, bad: np.ndarray) -> np.ndarray:
    """Per image: how much of the match goes to the good sentences (0..1)."""
    logits = 100.0 * images @ np.vstack([good, bad]).T
    logits -= logits.max(axis=1, keepdims=True)
    p = np.exp(logits)
    p /= p.sum(axis=1, keepdims=True)
    return p[:, :len(good)].sum(axis=1)


def pick(options: list[list[str]], score: dict[str, float], used: dict[tuple, int], previous: tuple | None,
         variety: float) -> list[str]:
    """The best-scoring framing; one already shown in this reel loses a little each time."""
    def value(names: list[str]) -> float:
        key = tuple(names)
        return float(np.mean([score[n] for n in names])) - variety * used.get(key, 0) - (variety if key == previous else 0.0)
    return max(options, key=value)


def choose_local(items: list[tuple[Shot, list[list[str]], dict[str, Path]]], profile: Profile, progress=print) -> dict[int, tuple[list[str], str]]:
    cfg = profile.ai_framing
    clip = Clip(ensure_model(progress))
    good, bad = clip.texts(cfg.good), clip.texts(cfg.bad)
    out: dict[int, tuple[list[str], str]] = {}
    used: dict[tuple, int] = {}
    previous = None
    for shot, options, stills in items:
        names = sorted(stills)
        score = dict(zip(names, clip_scores(clip.images([stills[n] for n in names]), good, bad)))
        best = pick(options, score, used, previous, cfg.variety)
        previous = tuple(best)
        used[previous] = used.get(previous, 0) + 1
        out[shot.index] = (best, "local: " + ", ".join(f"{n} {score[n]:.2f}" for n in names))
    return out


# --- remote engine: OpenRouter -------------------------------------------------------------
def list_models(timeout: float = 15.0) -> list[dict]:
    """Models on OpenRouter that accept pictures: [{"id", "name", "reasoning", "prompt_usd_per_m"}], newest first.
    The list is public; no key is sent."""
    import httpx
    try:
        data = httpx.get("https://openrouter.ai/api/v1/models", timeout=timeout).json()["data"]
    except (httpx.HTTPError, ValueError, KeyError) as error:
        raise Unavailable(f"the model list of OpenRouter cannot be read ({type(error).__name__})") from None
    out = []
    for m in sorted(data, key=lambda m: -(m.get("created") or 0)):
        if "image" not in (m.get("architecture") or {}).get("input_modalities", []):
            continue
        try:
            price = round(float((m.get("pricing") or {}).get("prompt") or 0) * 1e6, 2)
        except (TypeError, ValueError):
            price = 0.0
        out.append({"id": m["id"], "name": m.get("name") or m["id"],
                    "reasoning": "reasoning" in (m.get("supported_parameters") or []), "prompt_usd_per_m": price})
    return out


def api_key() -> str:
    key = os.environ.get(KEY_ENV, "")
    env = PROFILES_DIR.parent / ".env"
    if not key and env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == KEY_ENV:
                key = value.strip().strip("\"'")
    if not key:
        raise Unavailable(f"no {KEY_ENV} in the environment or in .env")
    return key


def sheet(stills: dict[str, Path], letters: dict[str, str]) -> bytes:
    """The candidate stills of one shot side by side, each with its letter."""
    from PIL import Image, ImageDraw
    names = list(stills)
    tile = 256
    image = Image.new("RGB", (tile * len(names), tile), "black")
    draw = ImageDraw.Draw(image)
    for i, name in enumerate(names):
        image.paste(Image.open(stills[name]).convert("RGB").resize((tile, tile)), (i * tile, 0))
        draw.rectangle([i * tile, 0, i * tile + 34, 30], fill="black")
        draw.text((i * tile + 10, 6), letters[name], fill="white")
    data = io.BytesIO()
    image.save(data, "JPEG", quality=80)
    return data.getvalue()


def _letters(names: list[str]) -> dict[str, str]:
    return {name: chr(ord("A") + i) for i, name in enumerate(names)}


def _image(data: bytes) -> dict:
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")}}


def request_body(batch: list[dict], profile: Profile, mount: Mount, title: str, showy: bool,
                 fixed: list[str] | None = None, so_far: list[str] | None = None) -> dict:
    """batch: per shot {"shot", "names", "extra", "stills", "pano"}."""
    cfg = profile.ai_framing
    flight = profile.kind == "flight"
    kind = ("a glider flight; the camera is fixed in the cockpit in front of the pilot" if flight
            else "a holiday; the camera is on a selfie stick carried by one of the travellers")
    who = "the pilot" if flight else "the person holding the camera"
    frame = ("yaw 0 is the nose of the glider (straight ahead), +90 the right wing, -90 the left wing, +-180 backwards "
             "at the pilot; pitch 0 is level, +90 straight up through the canopy, -90 straight down" if flight else
             "yaw is the angle round the camera and pitch 0 is the horizon (+90 straight up, -90 straight down at the "
             "ground under the stick); where the way ahead and the holder are is said per shot")
    text = (
        f"You are the director and editor of a short film \"{title}\" made from {kind}. "
        "The footage comes from an Insta360 camera that records the whole 360-degree sphere at once. Nothing was "
        "framed when it was shot: you aim the camera now, anywhere on the sphere, and you may move it during a "
        "shot. A film that only looks ahead and at the person could have been shot with any camera. Show what "
        "only a 360 camera can: what is below, above, beside and behind; follow something across the sphere; "
        "reveal where we are; play with scale.\n\n"
        "The cuts are already chosen. For every shot you get two pictures:\n"
        "1. PANORAMA: the whole sphere at the middle of the shot, with a grid. " + frame + ". Look at it "
        "closely: find the most interesting things in this particular moment (the airfield or a town below, "
        "a river, clouds, the sun, another aircraft, the wing against the ground, a shadow, a building, people) "
        "and note at which yaw / pitch they are.\n"
        "2. READY VIEWS: a few prepared views of the same moment, each with a letter and a line saying where it "
        "looks. Use them when they fit; you are not limited to them.\n\n"
        "Compose the camera path of each shot from one, two or three views in order; the camera travels smoothly "
        f"from each view to the next (at least {MIN_VIEW_S:g} s per view, so short shots take fewer views). A view "
        "is either the letter of a ready view, or your own:\n"
        "  {\"yaw\": deg, \"pitch\": deg, \"fov\": deg, \"projection\": \"normal\" | \"planet\" | \"fisheye\", "
        "\"zoom_to\": deg or null, \"pan\": deg, \"roll\": true/false, \"horizon\": \"level\" | \"aircraft\"}\n"
        "fov is the diagonal field of view: normal 60 (tele, picks out a detail) to 140 (very wide); planet 140 to "
        "320 (above 250 with pitch -90 the world wraps into a small planet; with pitch +90 it becomes a tunnel of "
        "sky); fisheye 160 to 230. zoom_to: the fov at the end of the view, for a slow push in (smaller) or pull "
        "back (larger). pan: degrees the camera turns to the right (negative: left) while the view is held, for a "
        "slow sweep. roll: the picture rolls once all the way over. horizon level keeps the horizon flat while the "
        "aircraft banks (clean outside views); aircraft lets the picture bank with it (the cabin in view). All "
        "views of one shot share a projection.\n\n"
        "Direct it like a film:\n"
        "- every shot shows something different; no two shots with the same path, no two neighbours that look "
        "the same way;\n"
        "- make moves mean something: start on a detail and pull back to reveal the whole, sweep from the wing "
        "tip along the horizon, rise from the ground straight below to the horizon, turn from the view to who is "
        "looking at it, let the small planet unroll into the view ahead;\n"
        "- rhythm: calm held views between shots that move; an opening that says where we are and an ending that "
        "feels like one;\n"
        f"- {who} is the main subject of at most {cfg.person_shots} shot(s) of the film, fixed shots included: "
        "this film is about the place and the motion, not a portrait;\n"
        "- avoid aiming at the instrument panel, at glare and reflections on the canopy, at empty sky with "
        "nothing in it;\n"
        + ("" if showy else
           f"- showpieces (planet or fisheye projection, a roll, ready views marked [accent]): at most {cfg.accents} "
           "shot(s) in the film, where the scene deserves it;\n")
        + "- give every shot an answer;\n"
        + "- write \"seen\" and \"why\" in Polish, one short plain sentence each: the film's author reads them.\n"
        + (f"{cfg.brief}\n" if cfg.brief else "")
        + ("\nFixed shots you cannot change: " + "; ".join(fixed) + "\n" if fixed else "")
        + ("\nAlready decided earlier in this film: " + "; ".join(so_far) + "\n" if so_far else "")
        + "\nAnswer with JSON only: {\"shots\": [{\"shot\": <number>, \"seen\": \"<what is worth showing in the "
          "panorama and where>\", \"views\": [<letter or view object>, ...], \"why\": \"<a few words>\"}]}")
    content = [{"type": "text", "text": text}]
    for item in batch:
        shot, names = item["shot"], item["names"]
        letters = _letters(names)
        lines = [f"Shot {shot.index}: {shot.type}, {shot.duration_s / shot.speed:.0f} s"
                 + (f", played {shot.speed:g}x faster" if shot.speed != 1 else "")
                 + f"; at most {max_views(shot)} view(s)."]
        if not flight:
            if "yaw_travel" in shot.attrs:
                lines.append(f"  The way ahead is near yaw {shot.attrs['yaw_travel']:.0f}.")
            if "yaw_holder" in shot.attrs:
                lines.append(f"  The person holding the camera is near yaw {shot.attrs['yaw_holder']:.0f}.")
        lines.append("  Ready views:")
        lines += [f"  {letters[n]}{' [accent]' if n in item['extra'] else ''}: {describe(n, mount, profile.kind)}" for n in names]
        lines.append("  Pictures: panorama, then ready views.")
        content.append({"type": "text", "text": "\n".join(lines)})
        content.append(_image(pano_sheet(item["pano"])))
        content.append(_image(sheet({n: item["stills"][n] for n in names}, letters)))
    body = {"model": cfg.model, "messages": [{"role": "user", "content": content}]}
    if cfg.reasoning_tokens > 0:                # thinking models want the default temperature and room to answer
        body["reasoning"] = {"max_tokens": cfg.reasoning_tokens}
        body["max_tokens"] = cfg.reasoning_tokens + ANSWER_TOKENS
    else:
        body["temperature"] = 0.9
    return body


def parse_reply(text: str, batch: list[dict], profile: Profile, mount: Mount, showy: bool,
                accents_left: int, person_left: int) -> dict[int, tuple[list[str], str, dict]]:
    """Per shot: (view names in order, why, the model's own views by name). Camera paths
    that are not valid for their shot are ignored; showpieces and person shots are rationed."""
    match = re.search(r"\{.*\}", text, re.S)
    try:
        rows = json.loads(match.group(0))["shots"] if match else []
    except (ValueError, KeyError, TypeError):
        rows = []
    known = {item["shot"].index: item for item in batch}
    out = {}
    for row in rows if isinstance(rows, list) else []:
        try:
            item = known[int(row["shot"])]
            shot, names = item["shot"], item["names"]
            by_letter = {letter: name for name, letter in _letters(names).items()}
            given = list(row["views"])[:max_views(shot)]        # a path too long for the shot loses its tail
            hold = shot.duration_s / max(len(given), 1)
            presets = {n: mount.presets[n] for n in names}
            path, own = [], {}
            for i, view in enumerate(given):
                if isinstance(view, dict):
                    name = f"ai{shot.index}{chr(97 + i)}"
                    presets[name] = custom_preset(view, profile, hold)
                    own[name] = presets[name].model_dump(exclude_defaults=True) | {"yaw": presets[name].yaw,
                                                                                    "pitch": presets[name].pitch,
                                                                                    "fov": presets[name].fov}
                else:
                    name = by_letter[str(view).strip().upper()]
                path.append(name)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        # The person is rationed: views of them beyond the limit are dropped from the path.
        if any(shows_person(presets[n], mount) for n in path):
            if person_left <= 0:
                path = [n for n in path if not shows_person(presets[n], mount)]
            else:
                person_left -= 1
        if not valid_sequence(path, shot, presets):
            continue
        accent = any(n in item["extra"] or (n in own and (presets[n].projection != "flat" or presets[n].roll_turns))
                     for n in path)
        if accent and not showy:
            if accents_left <= 0:
                continue
            accents_left -= 1
        why = " | ".join(str(row.get(k, "")).strip() for k in ("seen", "why") if row.get(k))[:240]
        out[shot.index] = (path, why, {n: own[n] for n in path if n in own})
    return out


def review_body(first: dict, batch: list[dict], found: dict, shown: dict[int, dict[str, Path]]) -> dict:
    """A second look: the model sees what its own views show and may aim again."""
    content = list(first["messages"][0]["content"])
    content.append({"type": "text", "text":
                    "REVIEW. You answered:\n" + json.dumps({"shots": [
                        {"shot": i, "views": [n if n not in own else own[n] for n in path]}
                        for i, (path, _, own) in sorted(found.items())]}) + "\n"
                    "You aimed some views yourself without seeing the result. Below is what each of them really "
                    "shows (numbered in the order of the path of the shot). Be strict: a view that turned out to "
                    "show the body or legs of the pilot, the instrument panel, a camera mount or tablet filling "
                    "the frame, glare, or simply not the thing you meant, must be aimed again (move the yaw / pitch "
                    "away from the obstacle, or tighten the fov onto the subject) or replaced by a ready view. "
                    "Views that work stay as they are. Answer with the same JSON for the whole film, every shot "
                    "included."})
    for item in batch:
        shot = item["shot"]
        if shot.index in shown:
            stills = shown[shot.index]
            content.append({"type": "text", "text": f"Shot {shot.index}, your own views: "
                            + ", ".join(f"{i + 1} = {name}" for i, name in enumerate(stills))})
            content.append(_image(sheet(stills, {name: str(i + 1) for i, name in enumerate(stills)})))
    return {**first, "messages": [{"role": "user", "content": content}]}


def choose_openrouter(items: list[dict], profile: Profile, mount: Mount, title: str, showy: bool,
                      fixed: list[tuple[str, bool]], post=None, log: Path | None = None,
                      own_stills=None) -> dict[int, tuple[list[str], str, dict]]:
    """fixed: (description, shows the person) of the shots the model cannot change.
    own_stills(shot, views) renders the views the model aimed itself, for the review round."""
    key = api_key()
    if post is None:
        import httpx

        def post(body: dict) -> str:
            try:
                reply = httpx.post(OPENROUTER_URL, json=body, timeout=300,
                                   headers={"Authorization": f"Bearer {key}", "X-Title": "insta-roller"})
            except httpx.HTTPError as error:
                raise Unavailable(f"OpenRouter cannot be reached ({type(error).__name__})") from None
            if reply.status_code != 200:
                raise Unavailable(f"OpenRouter answered {reply.status_code}: {reply.text[:200]}")
            data = reply.json()
            if "choices" not in data:           # OpenRouter reports some errors with status 200
                raise Unavailable(f"OpenRouter: {str(data.get('error', data))[:200]}")
            return data["choices"][0]["message"]["content"] or ""
    cfg = profile.ai_framing
    out: dict[int, tuple[list[str], str, dict]] = {}
    accents = cfg.accents
    person = cfg.person_shots - sum(shows for _, shows in fixed)
    so_far: list[str] = []
    said = []
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        body = request_body(batch, profile, mount, title, showy, [text for text, _ in fixed], so_far)
        reply = post(body)
        said.append(reply)
        found = parse_reply(reply, batch, profile, mount, showy, accents, person)
        shown = {}
        if own_stills is not None:
            by_index = {item["shot"].index: item["shot"] for item in batch}
            shown = {index: own_stills(by_index[index], own) for index, (_, _, own) in found.items() if own}
        if shown:                               # one more round with its own views in front of the model
            try:
                reply = post(review_body(body, batch, found, shown))
                said.append(reply)
                again = parse_reply(reply, batch, profile, mount, showy, accents, person)
                found = {**found, **again}
            except Unavailable:
                pass                            # the first answer stands
        for item in batch:
            shot = item["shot"]
            if shot.index not in found:
                continue
            path, why, own = found[shot.index]
            presets = {**mount.presets, **{n: Preset.model_validate(v) for n, v in own.items()}}
            person -= any(shows_person(presets[n], mount) for n in path)
            accents -= any(n in item["extra"] or presets[n].projection != "flat" or presets[n].roll_turns for n in path
                           if n in own or n in item["extra"])
            so_far.append(f"shot {shot.index} ({shot.type}): " + " -> ".join(
                n if n not in own else f"own view yaw {own[n]['yaw']:.0f} pitch {own[n]['pitch']:.0f}" for n in path))
            out[shot.index] = (path, f"{cfg.model}: {why}", own)
    if log is not None:                         # kept beside the cache, to see what the model said
        log.write_text("\n\n".join(said), encoding="utf-8")
    return out


# --- entry point --------------------------------------------------------------------------
def apply(reel: ReelPlan, scenes: list[Scene], profile: Profile, mount: Mount, theme: Theme | None, ctx,
          progress=print, post=None) -> ReelPlan:
    """The reel with the framing of its shots chosen by the engine in profile.ai_framing."""
    cfg = profile.ai_framing
    if cfg.engine == "off":
        return reel
    by_id = {s.id: s for s in scenes}
    themes = []
    for name in profile.themes:
        try:
            themes.append(load_theme(name))
        except OSError:
            pass
    remote = cfg.engine == "openrouter"
    plans = []
    for shot in reel.shots:
        scene = by_id.get(shot.scene_id)
        options = candidates(shot, scene, profile, theme, mount, themes)
        names, extra = views(shot, scene, profile, theme, mount, themes) if remote else ([], set())
        if len(options) > 1 or (remote and scene is not None and shot.type in profile.scene_types):
            plans.append((shot, options, names, extra))
    if not plans:
        return reel
    folder = ctx.work / "ai_framing"
    folder.mkdir(parents=True, exist_ok=True)
    key = fingerprint(cfg.model_dump_json(), reel.theme, reel.title, mount.model_dump_json(), PROMPT,
                      json.dumps([[s.scene_id, s.start_s, s.duration_s, s.speed, s.attrs, o, n, sorted(e)]
                                  for s, o, n, e in plans], sort_keys=True))
    cache = folder / f"{key}.json"
    if cache.exists():
        chosen = {int(k): (v[0], v[1], v[2] if len(v) > 2 else {})
                  for k, v in json.loads(cache.read_text(encoding="utf-8")).items()}
    else:
        try:
            if remote:
                items = [{"shot": shot, "names": names, "extra": extra, "stills": _stills(shot, names, ctx, folder),
                          "pano": _pano(shot, ctx, folder)} for shot, _, names, extra in plans]
                showy = bool(theme) and any(not _plain(o, mount) for options in theme.variants.values() for o in options
                                            if all(n in mount.presets for n in o))
                open_ids = {shot.index for shot, _, _, _ in plans}
                fixed = [(f"shot {s.index} ({s.type}): " + " -> ".join(s.framing),
                          any(shows_person(mount.presets[n], mount) for n in s.framing if n in mount.presets))
                         for s in reel.shots if s.index not in open_ids]
                chosen = choose_openrouter(items, profile, mount, reel.title or reel.theme, showy, fixed, post,
                                           folder / f"{key}.reply.txt",
                                           lambda shot, own: _own_stills(shot, own, ctx, folder))
            else:
                items = [(shot, options, _stills(shot, sorted({n for o in options for n in o}), ctx, folder))
                         for shot, options, _, _ in plans]
                chosen = {k: (names, why, {}) for k, (names, why) in choose_local(items, profile, progress).items()}
        except (Unavailable, RuntimeError) as error:
            progress(f"ai framing: {error}; the theme's own framing is used")
            return reel
        cache.write_text(json.dumps(chosen, ensure_ascii=False), encoding="utf-8")
    shots = []
    for shot in reel.shots:
        if shot.index in chosen:
            names, why, own = chosen[shot.index]
            attrs = {**shot.attrs, "ai": why}
            if own:
                attrs["views"] = own            # the views the model aimed itself, used by plan_framing
            shot = shot.model_copy(update={"framing": list(names), "attrs": attrs})
        shots.append(shot)
    return reel.model_copy(update={"shots": shots})
