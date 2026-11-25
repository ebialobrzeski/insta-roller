"""Known people: face vectors exported from Immich, recognition, targeting rules.

Everything except the export works from the exported file alone, so an Immich
update can at worst break `faces-export`. Without insightface / onnxruntime or
without the file every function degrades to "no people".

The export file (people.json, format version 1):
    {"format": "insta-roller-people", "version": 1, "model": "buffalo_l",
     "exported_at": "...", "source": "db" | "api", "immich_version": "2.3.1" | null,
     "people": [{"name": "Ania", "immich_ids": ["..."], "embeddings": [[512 floats], ...]}]}
Embeddings are L2-normalised; the distance is the cosine distance (Immich's
default limit for buffalo_l is 0.5).

What is known about Immich (checked against the server source, November 2026):
  * The REST API does not return face embeddings (PersonResponseDto and
    AssetFaceResponseDto carry names and bounding boxes only).
  * Embeddings live in Postgres: face_search("faceId", embedding vector(512)),
    joined to the face table. The face table is `asset_face` (`asset_faces`
    before the 2025 rename) and links to the person by "personId" (older) or
    "personGroupId" (current main, where `person` is keyed by "personGroupId").
    Very old versions kept the embedding in asset_faces.embedding. The export
    reads information_schema first and builds its query from what it finds.
  * API routes used by the fallback: GET /api/people (page, size, withHidden),
    GET /api/people/{id}/thumbnail, POST /api/search/metadata (personIds; newer
    servers: filter.personIds.any), GET /api/assets/{id}/thumbnail?size=preview,
    GET /api/faces?id={assetId}, GET /api/server/version.
Not verified against a live server: the route details above on any particular
Immich version, and where a non-default model name is stored (the DB export
looks for a `system-config` override in system_metadata, best effort).
"""
from __future__ import annotations

import json
import math
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

import numpy as np

FORMAT = "insta-roller-people"
VERSION = 1
DEFAULT_MODEL = "buffalo_l"         # Immich's default facial-recognition model
DEFAULT_THRESHOLD = 0.5             # Immich's default maximum cosine distance
MAX_PER_PERSON = 50


class FacesError(RuntimeError):
    pass


class ModelMismatch(FacesError):
    """The export was made with another recognition model: vectors are not comparable."""


class ExportError(FacesError):
    pass


# --- the exported file ---------------------------------------------------------
@dataclass
class People:
    model: str = DEFAULT_MODEL
    vectors: dict[str, np.ndarray] = field(default_factory=dict)    # name -> (n, dim), unit rows
    exported_at: str = ""
    immich_version: str | None = None
    source: str = ""

    @property
    def names(self) -> list[str]:
        return list(self.vectors)

    def __bool__(self) -> bool:
        return bool(self.vectors)


def _unit(rows) -> np.ndarray:
    a = np.atleast_2d(np.asarray(rows, dtype=np.float32))
    norm = np.linalg.norm(a, axis=1, keepdims=True)
    return a / np.where(norm > 0, norm, 1.0)


def load_people(path: str | Path, model: str | None = None) -> People:
    """Read an export. With `model` given, refuse a file made with another model."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise FacesError(f"Cannot read the people file {path}: {e}") from e
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise FacesError(f"{path} is not an insta-roller people file.")
    if data.get("version") != VERSION:
        raise FacesError(f"{path} has format version {data.get('version')}; this program reads version {VERSION}. "
                         "Run faces-export again.")
    people = People(model=str(data.get("model", "")), exported_at=str(data.get("exported_at", "")),
                    immich_version=data.get("immich_version"), source=str(data.get("source", "")))
    for person in data.get("people", []):
        rows = person.get("embeddings") or []
        if rows:
            people.vectors[str(person["name"])] = _unit(rows)
    if model is not None:
        check_model(people, model)
    return people


def try_load(path: str | Path | None, model: str | None = None, warn=print) -> People | None:
    """load_people for the pipeline: None (and one warning line) instead of an error."""
    if not path or not Path(path).exists():
        return None
    try:
        return load_people(path, model)
    except FacesError as e:
        warn(f"faces: {e} Continuing without people.")
        return None


def save_people(path: str | Path, vectors: dict[str, np.ndarray], model: str, source: str,
                immich_version: str | None = None, ids: dict[str, list[str]] | None = None) -> Path:
    path = Path(path)
    data = {"format": FORMAT, "version": VERSION, "model": model,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": source, "immich_version": immich_version,
            "people": [{"name": name, "immich_ids": (ids or {}).get(name, []),
                        "embeddings": [[round(float(x), 5) for x in row] for row in _unit(rows)]}
                       for name, rows in vectors.items()]}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")           # a failed export never damages the old file
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)
    return path


def check_model(people: People, model: str) -> None:
    if people.model != model:
        raise ModelMismatch(f"The people file was exported with model '{people.model}', the local model is "
                            f"'{model}'. Vectors of different models cannot be compared.")


# --- matching ------------------------------------------------------------------
def distances(embedding, people: People, k: int = 3) -> dict[str, float]:
    """Cosine distance to every person: the mean of the k closest of their vectors."""
    v = _unit(embedding)[0]
    out = {}
    for name, rows in people.vectors.items():
        if rows.shape[1] != v.shape[0]:
            raise ModelMismatch(f"Vector length {v.shape[0]} does not fit the export ({rows.shape[1]}).")
        d = np.sort(1.0 - rows @ v)[:max(1, k)]
        out[name] = float(d.mean())
    return out


def match(embedding, people: People | None, threshold: float = DEFAULT_THRESHOLD, k: int = 3,
          model: str | None = None) -> tuple[str | None, float]:
    """(name, distance) of the closest person, or (None, distance) when nobody is
    within the threshold. `model` = the model that made `embedding`."""
    if not people:
        return None, math.inf
    if model is not None:
        check_model(people, model)
    dist = distances(embedding, people, k)
    name = min(dist, key=dist.get)
    return (name if dist[name] <= threshold else None), dist[name]


# --- detection -----------------------------------------------------------------
@dataclass
class Face:
    bbox: tuple[float, float, float, float]     # x1, y1, x2, y2 in pixels
    score: float
    embedding: np.ndarray                       # unit length


def available() -> bool:
    """True when insightface and onnxruntime can be imported."""
    try:
        import insightface  # noqa: F401
        import onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def _image(image):
    if isinstance(image, (str, Path)):
        import cv2
        data = np.fromfile(str(image), dtype=np.uint8)      # imread fails on non-ASCII paths on Windows
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image is None:
            raise FacesError("Cannot read the image.")
    return image


class Detector:
    """insightface FaceAnalysis (SCRFD detection + ArcFace vectors), loaded on first use.
    Images are paths or BGR arrays, as OpenCV reads them."""

    def __init__(self, model: str = DEFAULT_MODEL, det_size: int = 640, min_score: float = 0.5,
                 providers: list[str] | None = None, root: str | None = None):
        self.model, self.det_size, self.min_score = model, det_size, min_score
        self.providers = providers or ["CPUExecutionProvider"]
        self.root = root
        self._app = None

    @staticmethod
    def available() -> bool:
        return available()

    def _load(self):
        if self._app is None:
            if not available():
                raise FacesError("Face recognition needs insightface and onnxruntime: pip install insta-roller[faces]")
            import warnings
            from insightface.app import FaceAnalysis
            # insightface calls a scikit-image function that is deprecated since 0.26 (gone in 2.2):
            # it still works, and the warning only clutters the job log the user reads.
            warnings.filterwarnings("ignore", category=FutureWarning, module=r"insightface\..*")
            kwargs = {"root": self.root} if self.root else {}
            app = FaceAnalysis(name=self.model, allowed_modules=["detection", "recognition"],
                               providers=self.providers, **kwargs)
            app.prepare(ctx_id=0, det_size=(self.det_size, self.det_size), det_thresh=self.min_score)
            self._app = app
        return self._app

    def detect(self, image) -> list[Face]:
        faces = self._load().get(_image(image))
        return [Face(tuple(float(x) for x in f.bbox), float(f.det_score), _unit(f.normed_embedding)[0])
                for f in faces if getattr(f, "normed_embedding", None) is not None]


# --- sphere geometry -------------------------------------------------------------
def _direction(yaw: float, pitch: float) -> np.ndarray:
    """Unit vector: x right, y up, z forward."""
    y, p = math.radians(yaw), math.radians(pitch)
    return np.array([math.cos(p) * math.sin(y), math.sin(p), math.cos(p) * math.cos(y)])


def _angles(v) -> tuple[float, float]:
    return (math.degrees(math.atan2(v[0], v[2])),
            math.degrees(math.asin(max(-1.0, min(1.0, v[1] / np.linalg.norm(v))))))


def _focal(width: float, height: float, fov: float) -> float:
    """Focal length in pixels of a flat view with the diagonal field of view `fov`."""
    return math.hypot(width, height) / 2 / math.tan(math.radians(min(fov, 179.0)) / 2)


def view_to_sphere(x: float, y: float, width: int, height: int, view_yaw: float, view_pitch: float,
                   fov: float) -> tuple[float, float]:
    """Direction (yaw, pitch) of the pixel (x, y) of a flat view rendered like
    core.render.flat_filter: yaw + to the right, pitch + up, diagonal fov, no roll."""
    rx, ry, rz = x - width / 2, height / 2 - y, _focal(width, height, fov)
    p, w = math.radians(view_pitch), math.radians(view_yaw)
    ry, rz = ry * math.cos(p) + rz * math.sin(p), -ry * math.sin(p) + rz * math.cos(p)
    rx, rz = rx * math.cos(w) + rz * math.sin(w), -rx * math.sin(w) + rz * math.cos(w)
    return _angles((rx, ry, rz))


def sphere_to_view(yaw: float, pitch: float, width: int, height: int, view_yaw: float, view_pitch: float,
                   fov: float) -> tuple[float, float] | None:
    """Pixel of a direction in a flat view; None when it is behind the view."""
    rx, ry, rz = _direction(yaw, pitch)
    p, w = math.radians(view_pitch), math.radians(view_yaw)
    rx, rz = rx * math.cos(w) - rz * math.sin(w), rx * math.sin(w) + rz * math.cos(w)
    ry, rz = ry * math.cos(p) - rz * math.sin(p), ry * math.sin(p) + rz * math.cos(p)
    if rz <= 1e-6:
        return None
    f = _focal(width, height, fov)
    return width / 2 + f * rx / rz, height / 2 - f * ry / rz


def angle_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Angle in degrees between two (yaw, pitch) directions."""
    return math.degrees(math.acos(max(-1.0, min(1.0, float(_direction(*a) @ _direction(*b))))))


def _centre(points: list[tuple[float, float]]) -> tuple[float, float]:
    return _angles(sum(_direction(*p) for p in points))


# --- sightings -------------------------------------------------------------------
@dataclass
class Sighting:
    t: float
    yaw: float
    pitch: float
    name: str | None            # None = a stranger
    size: float                 # face height in degrees
    distance: float = math.inf  # cosine distance to `name`, or to the closest person
    track: int = -1
    score: float = 0.0


def scan(views, people: People | None, detector: Detector | None = None, threshold: float = DEFAULT_THRESHOLD,
         keep_distance: float = 0.7, max_step_deg: float = 25.0, max_gap_s: float = 3.0,
         merge_deg: float = 6.0, k: int = 3) -> list[Sighting]:
    """Detect, recognise and track faces. `views` yields (t, image, view_yaw,
    view_pitch, fov): flat views cut from the 360 picture.

    A track is a chain of detections whose direction moves less than
    `max_step_deg` between samples at most `max_gap_s` apart. Once a face of a
    track is recognised the whole track carries that name, so glasses or a cap
    do not lose the person; a face further than `keep_distance` from the person
    is still left a stranger. Returns [] without people or without insightface.
    """
    if not people:
        return []
    detector = detector or Detector(people.model)
    if not detector.available():
        return []
    check_model(people, detector.model)

    frames: dict[float, list[tuple[Sighting, dict[str, float]]]] = {}
    for t, image, view_yaw, view_pitch, fov in views:
        image = _image(image)
        height, width = image.shape[:2]
        focal = _focal(width, height, fov)
        for face in detector.detect(image):
            x1, y1, x2, y2 = face.bbox
            yaw, pitch = view_to_sphere((x1 + x2) / 2, (y1 + y2) / 2, width, height, view_yaw, view_pitch, fov)
            dist = distances(face.embedding, people, k)
            best = min(dist, key=dist.get)
            s = Sighting(float(t), round(yaw, 2), round(pitch, 2), best if dist[best] <= threshold else None,
                         round(math.degrees(2 * math.atan((y2 - y1) / 2 / focal)), 2), round(dist[best], 4),
                         score=round(face.score, 3))
            frames.setdefault(float(t), []).append((s, dist))

    tracks: list[list[tuple[Sighting, dict[str, float]]]] = []
    names: list[str | None] = []
    for t in sorted(frames):
        kept: list[tuple[Sighting, dict[str, float]]] = []
        for item in sorted(frames[t], key=lambda i: -i[0].score):       # the same face seen in two views
            if all(angle_between((item[0].yaw, item[0].pitch), (o[0].yaw, o[0].pitch)) > merge_deg for o in kept):
                kept.append(item)
        pairs = []
        for i, (s, _) in enumerate(kept):
            for j, track in enumerate(tracks):
                last = track[-1][0]
                step = angle_between((s.yaw, s.pitch), (last.yaw, last.pitch))
                clash = s.name and names[j] and s.name != names[j]
                if step <= max_step_deg and 0 < t - last.t <= max_gap_s and not clash:
                    pairs.append((step, i, j))
        taken_s, taken_t = set(), set()
        for _, i, j in sorted(pairs):
            if i not in taken_s and j not in taken_t:
                taken_s.add(i), taken_t.add(j)
                tracks[j].append(kept[i])
                names[j] = names[j] or kept[i][0].name
        for i, item in enumerate(kept):
            if i not in taken_s:
                tracks.append([item])
                names.append(item[0].name)

    out = []
    for j, track in enumerate(tracks):
        for s, dist in track:
            s.track = j
            if s.name is None and names[j] and dist[names[j]] <= keep_distance:
                s.name, s.distance = names[j], round(dist[names[j]], 4)
            out.append(s)
    return sorted(out, key=lambda s: (s.t, s.track))


def scan_recording(rec, mount, times, people: People | None, work: Path, log: Path,
                   detector: Detector | None = None, level: tuple[float, float] | None = None,
                   yaws=(0, 60, 120, 180, -120, -60), pitch: float = 0.0, fov: float = 100.0,
                   size: tuple[int, int] = (800, 800), **options) -> list[Sighting]:
    """scan() over flat stills of a recording (core.render.still) at the given times."""
    if not people or not (detector or Detector).available():
        return []
    from .config import Preset
    from .render import still

    def views():
        for t in times:
            for yaw in yaws:
                out = Path(work) / "faces" / f"{rec.id}_{t:09.2f}_{yaw:+04.0f}_{pitch:+03.0f}_{fov:.0f}.jpg"
                if not out.exists():
                    still(rec, mount, t, Preset(yaw=yaw, pitch=pitch, fov=fov), size, out, work, log, level=level)
                yield t, out, yaw, pitch, fov

    return scan(views(), people, detector, **options)


# --- targeting -------------------------------------------------------------------
@dataclass
class Target:
    yaw: float
    pitch: float
    fov: float
    names: list[str]


def _known(sightings, weights: dict[str, float] | None) -> dict[str, list[Sighting]]:
    """Sightings of the people that may be framed. Strangers never are; with
    weights given, neither is anyone missing from them or weighted 0."""
    out: dict[str, list[Sighting]] = {}
    for s in sightings:
        if s.name is None or (weights and weights.get(s.name, 0.0) <= 0):
            continue
        out.setdefault(s.name, []).append(s)
    return out


def deficits(weights: dict[str, float], shown_so_far: dict[str, float]) -> dict[str, float]:
    """Seconds each person is behind (+) their weighted share of the screen time."""
    total_w = sum(w for w in weights.values() if w > 0) or 1.0
    total = sum(shown_so_far.get(n, 0.0) for n, w in weights.items() if w > 0)
    return {n: w / total_w * total - shown_so_far.get(n, 0.0) for n, w in weights.items() if w > 0}


def choose_target(sightings, weights: dict[str, float] | None = None, shown_so_far: dict[str, float] | None = None,
                  group_angle: float = 40.0, person_fov: float = 75.0, max_fov: float = 130.0) -> Target | None:
    """Where to point the camera for the given sightings (one moment or one scene).

    Known people within `group_angle` of each other get a group shot: centred
    between them and wider. Otherwise the person furthest behind their weighted
    share of screen time is framed. None when no known person is in sight.
    """
    known = _known(sightings, weights)
    if not known:
        return None
    weights = {n: (weights or {}).get(n, 1.0) for n in known} | {n: w for n, w in (weights or {}).items() if w > 0}
    behind = deficits(weights, shown_so_far or {})
    where = {n: _centre([(s.yaw, s.pitch) for s in ss]) for n, ss in known.items()}

    for count in range(len(where), 1, -1):
        groups = [g for g in combinations(sorted(where), count)
                  if all(angle_between(where[a], where[b]) <= group_angle for a, b in combinations(g, 2))]
        if groups:
            group = max(groups, key=lambda g: sum(behind[n] for n in g))
            yaw, pitch = _centre([where[n] for n in group])
            spread = max(angle_between(where[a], where[b]) for a, b in combinations(group, 2))
            return Target(round(yaw, 2), round(pitch, 2), round(min(max_fov, person_fov + spread), 1), list(group))

    name = max(where, key=lambda n: (round(behind[n], 6), weights[n], max(s.size for s in known[n])))
    return Target(round(where[name][0], 2), round(where[name][1], 2), person_fov, [name])


def screen_time(shown_so_far: dict[str, float], target: Target | None, duration_s: float) -> dict[str, float]:
    """The screen-time account after showing `target` for `duration_s`."""
    shown = dict(shown_so_far)
    for name in (target.names if target else []):
        shown[name] = shown.get(name, 0.0) + duration_s
    return shown


def scene_bonus(sightings, person_bonus: float, weights: dict[str, float] | None = None) -> float:
    """Score added to a scene in which a known person is seen."""
    return person_bonus if _known(sightings, weights) else 0.0


# --- export from Immich ----------------------------------------------------------
def _pick(rows, cap: int) -> np.ndarray:
    """At most `cap` vectors: the outer tenth is dropped (wrongly tagged faces),
    the rest sampled evenly from typical to unusual."""
    rows = _unit(rows)
    if len(rows) <= cap:
        return rows
    centre = _unit(rows.mean(axis=0))[0]
    order = np.argsort(1.0 - rows @ centre)[:max(cap, int(len(rows) * 0.9))]
    return rows[order[np.linspace(0, len(order) - 1, cap).round().astype(int)]]


def _wanted(names: list[str]) -> dict[str, str]:
    wanted = {n.strip().lower(): n.strip() for n in names if n.strip()}
    if not wanted:
        raise ExportError("No people named. Pass --people \"Name,Name\".")
    return wanted


def _vector(text) -> list[float]:
    if isinstance(text, str):                   # pgvector as text: "[0.1,0.2,...]"
        return [float(x) for x in text.strip("[]").split(",")]
    return [float(x) for x in text]


FACE_TABLES = ("asset_face", "asset_faces")
SAMPLE_PER_PERSON = 500


def export_from_cursor(cur, names: list[str], cap: int = MAX_PER_PERSON, model: str = DEFAULT_MODEL
                       ) -> tuple[dict[str, np.ndarray], dict[str, list[str]], str | None]:
    """Read the vectors of the named people through a DB-API cursor (SELECT only).
    Returns (vectors by name, Immich person ids by name, Immich version)."""
    wanted = _wanted(names)
    tables = ("person", "face_search", "system_metadata", "version_history", *FACE_TABLES)
    cur.execute("SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = ANY(current_schemas(false)) AND table_name = ANY(%s)", (list(tables),))
    schema: dict[str, set[str]] = {}
    for table, column in cur.fetchall():
        schema.setdefault(table, set()).add(column)

    face = next((t for t in FACE_TABLES if t in schema), None)
    person = schema.get("person", set())
    if face is None or "name" not in person:
        raise ExportError("The Immich tables person / asset_face were not found: the schema of this Immich "
                          "version is not known. Use the API export (leave out --db-url).")
    if "personId" in schema[face] and "id" in person:
        link, key = "personId", "id"
    elif "personGroupId" in schema[face] and "personGroupId" in person:
        link = key = "personGroupId"
    else:
        raise ExportError(f"Cannot tell how {face} links to person in this Immich version. "
                          "Use the API export (leave out --db-url).")
    if {"faceId", "embedding"} <= schema.get("face_search", set()):
        source, vector = f'JOIN face_search s ON s."faceId" = f."id"', 's."embedding"'
    elif "embedding" in schema[face]:
        source, vector = "", 'f."embedding"'
    else:
        raise ExportError("No face vectors found (face_search.embedding). Use the API export (leave out --db-url).")
    where = "".join(f' AND f."{c}" {test}' for c, test in (("deletedAt", "IS NULL"), ("isVisible", "IS TRUE"))
                    if c in schema[face])

    if {"key", "value"} <= schema.get("system_metadata", set()):
        cur.execute("""SELECT "value"::text FROM system_metadata WHERE "key" = %s""", ("system-config",))
        for (value,) in cur.fetchall():
            try:
                used = json.loads(value)["machineLearning"]["facialRecognition"]["modelName"]
            except (KeyError, TypeError, ValueError):
                continue
            if used != model:
                raise ModelMismatch(f"Immich recognises faces with '{used}', the export was asked for '{model}'. "
                                    f"Pass --model {used}.")
    version = None
    if {"version", "createdAt"} <= schema.get("version_history", set()):
        cur.execute('SELECT "version" FROM version_history ORDER BY "createdAt" DESC LIMIT 1')
        row = cur.fetchall()
        version = str(row[0][0]) if row else None

    cur.execute(f'SELECT p."{key}"::text, p."name" FROM person p WHERE lower(p."name") = ANY(%s)', (list(wanted),))
    found = [(pid, wanted[name.lower()]) for pid, name in cur.fetchall()]
    vectors: dict[str, list] = {}
    ids: dict[str, list[str]] = {}
    for pid, name in found:
        if pid in ids.setdefault(name, []):
            continue
        ids[name].append(pid)
        cur.execute(f'SELECT {vector}::text FROM {face} f {source} WHERE f."{link}"::text = %s{where} '
                    f'ORDER BY md5(f."id"::text) LIMIT {SAMPLE_PER_PERSON}', (pid,))
        vectors.setdefault(name, []).extend(_vector(row[0]) for row in cur.fetchall())
    return _finish(vectors, wanted, cap), ids, version


def _finish(vectors: dict[str, list], wanted: dict[str, str], cap: int) -> dict[str, np.ndarray]:
    missing = [n for n in wanted.values() if not vectors.get(n)]
    if missing:
        raise ExportError("No face vectors in Immich for: " + ", ".join(missing) + ". Check the names.")
    return {n: _pick(vectors[n], cap) for n in wanted.values()}


def export_db(db_url: str, names: list[str], cap: int = MAX_PER_PERSON, model: str = DEFAULT_MODEL):
    """export_from_cursor over a read-only connection. Needs psycopg (or psycopg2)."""
    try:
        import psycopg as driver
    except ImportError:
        try:
            import psycopg2 as driver
        except ImportError:
            raise ExportError("The database export needs psycopg: pip install \"psycopg[binary]\"") from None
    try:
        conn = driver.connect(db_url)
    except Exception as e:
        raise ExportError(f"Cannot connect to the Immich database ({type(e).__name__}).") from None
    try:
        if hasattr(conn, "set_session"):
            conn.set_session(readonly=True)         # psycopg2
        else:
            conn.read_only = True                   # psycopg 3
        cur = conn.cursor()
        try:
            return export_from_cursor(cur, names, cap, model)
        except FacesError:
            raise
        except Exception as e:
            raise ExportError(f"The Immich database did not answer as expected ({type(e).__name__}: {e}). "
                              "Use the API export (leave out --db-url).") from e
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def _fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


class Immich:
    """The few read-only Immich API calls of the export."""

    def __init__(self, url: str, api_key: str, fetch=_fetch):
        url = url.rstrip("/")
        self.base = (url[:-4] if url.endswith("/api") else url) + "/api"
        self._key, self._fetch = api_key, fetch

    def get(self, path: str, body: dict | None = None) -> bytes:
        headers = {"x-api-key": self._key, "Accept": "*/*"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        try:
            return self._fetch("POST" if body is not None else "GET", self.base + path, headers, data)
        except urllib.error.HTTPError as e:
            raise ExportError(f"Immich answered {e.code} to {path.split('?')[0]}") from None
        except (urllib.error.URLError, OSError) as e:
            raise ExportError(f"Cannot reach Immich at {self.base}: {getattr(e, 'reason', e)}") from None

    def json(self, path: str, body: dict | None = None):
        try:
            return json.loads(self.get(path, body))
        except ValueError:
            raise ExportError(f"Immich did not answer {path.split('?')[0]} with JSON") from None

    def version(self) -> str | None:
        for path in ("/server/version", "/server-info/version"):
            try:
                v = self.json(path)
                return f"{v['major']}.{v['minor']}.{v['patch']}"
            except (ExportError, KeyError, TypeError):
                continue
        return None

    def people(self) -> list[dict]:
        out, page = [], 1
        while page <= 200:
            data = self.json(f"/people?withHidden=true&size=500&page={page}")
            batch = data.get("people", []) if isinstance(data, dict) else data
            out += [p for p in batch if isinstance(p, dict) and p.get("id")]
            if not (isinstance(data, dict) and data.get("hasNextPage")) or not batch:
                break
            page += 1
        return out

    def person_assets(self, person_id: str, count: int) -> list[str]:
        bodies = ({"personIds": [person_id], "type": "IMAGE", "size": count},
                  {"filter": {"personIds": {"any": [person_id]}, "type": {"eq": "IMAGE"}}, "size": count})
        error = None
        for body in bodies:                     # the flat fields are deprecated in newer servers
            try:
                items = self.json("/search/metadata", body)["assets"]["items"]
                return [a["id"] for a in items if a.get("id")][:count]
            except (ExportError, KeyError, TypeError) as e:
                error = e
        raise ExportError(f"Immich search for a person's photos failed: {error}")

    def asset_faces(self, asset_id: str) -> list[dict]:
        try:
            faces = self.json(f"/faces?id={asset_id}")
            return faces if isinstance(faces, list) else []
        except ExportError:
            return []

    def preview(self, asset_id: str) -> bytes:
        return self.get(f"/assets/{asset_id}/thumbnail?size=preview")

    def person_thumbnail(self, person_id: str) -> bytes:
        return self.get(f"/people/{person_id}/thumbnail")


def _decode(data: bytes):
    import cv2
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)


def _overlap(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    return w * h / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - w * h)


def _face_of(person_id: str, faces: list[Face], boxes: list[dict], shape, reference, threshold: float) -> Face | None:
    """The detected face that belongs to the person: by Immich's bounding box,
    else by likeness to the person's thumbnail."""
    height, width = shape[:2]
    best, best_iou = None, 0.3
    for box in boxes:
        try:
            if (box.get("person") or {}).get("id") != person_id:
                continue
            sx, sy = width / box["imageWidth"], height / box["imageHeight"]
            rect = (box["boundingBoxX1"] * sx, box["boundingBoxY1"] * sy, box["boundingBoxX2"] * sx, box["boundingBoxY2"] * sy)
        except (KeyError, TypeError, ZeroDivisionError):
            continue
        for face in faces:
            iou = _overlap(face.bbox, rect)
            if iou > best_iou:
                best, best_iou = face, iou
    if best is None and reference is not None and faces:
        near = min(faces, key=lambda f: 1.0 - float(f.embedding @ reference))
        if 1.0 - float(near.embedding @ reference) <= threshold:
            best = near
    return best


def export_api(client: Immich, names: list[str], detector: Detector, cap: int = MAX_PER_PERSON,
               sample: int = 60, threshold: float = DEFAULT_THRESHOLD, progress=print
               ) -> tuple[dict[str, np.ndarray], dict[str, list[str]], str | None]:
    """Without database access: download photos of each person and compute the
    vectors locally with the same model. Same return value as export_from_cursor."""
    wanted = _wanted(names)
    vectors: dict[str, list] = {}
    ids: dict[str, list[str]] = {}
    for person in client.people():
        name = wanted.get(str(person.get("name", "")).strip().lower())
        if name is None:
            continue
        pid = person["id"]
        ids.setdefault(name, []).append(pid)
        rows = vectors.setdefault(name, [])
        reference = None
        try:
            thumb = detector.detect(_decode(client.person_thumbnail(pid)))
            if thumb:
                reference = max(thumb, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1])).embedding
                rows.append(reference)
        except (ExportError, FacesError):
            pass
        for asset_id in client.person_assets(pid, sample):
            try:
                image = _decode(client.preview(asset_id))
            except ExportError:
                continue
            if image is None:
                continue
            face = _face_of(pid, detector.detect(image), client.asset_faces(asset_id), image.shape, reference, threshold)
            if face is not None:
                rows.append(face.embedding)
        progress(f"faces-export: {name}: {len(rows)} faces")
    return _finish(vectors, wanted, cap), ids, client.version()


def export(names: list[str], out: str | Path, immich_url: str | None = None, api_key: str | None = None,
           db_url: str | None = None, model: str = DEFAULT_MODEL, cap: int = MAX_PER_PERSON,
           sample: int = 60, progress=print) -> People:
    """Export the named people to `out`: from the database when `db_url` is
    given, else through the API. The old file survives a failed export."""
    client = Immich(immich_url, api_key) if immich_url and api_key else None
    if db_url:
        vectors, ids, version = export_db(db_url, names, cap, model)
        version = (client.version() if client else None) or version
        source = "db"
    elif client:
        detector = Detector(model)
        if not detector.available():
            raise ExportError("Without --db-url the vectors are computed locally, which needs insightface and "
                              "onnxruntime: pip install insta-roller[faces]")
        vectors, ids, version = export_api(client, names, detector, cap, sample, progress=progress)
        source = "api"
    else:
        raise ExportError("Give --db-url, or --immich-url with the API key in the environment.")
    save_people(out, vectors, model, source, version, ids)
    return load_people(out)


# --- commands --------------------------------------------------------------------
def describe(people: People) -> str:
    lines = [f"model {people.model}, exported {people.exported_at or '?'} from Immich "
             f"{people.immich_version or '(version unknown)'} via {people.source or '?'}"]
    lines += [f"  {name:<20} {len(rows)} vectors of length {rows.shape[1]}" for name, rows in people.vectors.items()]
    return "\n".join(lines)


def cmd_export(args) -> None:
    key = os.environ.get(args.api_key_env, "") if args.immich_url else ""
    db_url = args.db_url or (os.environ.get(args.db_url_env, "") if args.db_url_env else "")
    if args.immich_url and not key and not db_url:
        sys.exit(f"The environment variable {args.api_key_env} with the Immich API key is not set.")
    try:
        people = export(args.people.split(","), args.out, args.immich_url, key, db_url, args.model,
                        args.max_per_person, args.sample)
    except FacesError as e:
        sys.exit(f"faces-export: {e}")
    print(f"faces-export: {args.out}\n{describe(people)}")


def cmd_check(args) -> None:
    try:
        people = load_people(args.file)
    except FacesError as e:
        sys.exit(f"faces-check: {e}")
    print(describe(people))
    if people.model != args.model:
        print(f"WARNING: the local model is '{args.model}': this file cannot be used with it.")
    if not available():
        print("insightface / onnxruntime are not installed: people will not be recognised "
              "(pip install insta-roller[faces]).")


def add_commands(sub) -> None:
    p = sub.add_parser("faces-export", help="export the face vectors of named people from Immich (read only)")
    p.add_argument("--people", required=True, help="comma-separated names, as they are written in Immich")
    p.add_argument("--out", default="people.json", help="the people file to write")
    p.add_argument("--immich-url", help="address of the Immich server, e.g. http://nas:2283")
    p.add_argument("--api-key-env", default="IMMICH_API_KEY",
                   help="name of the environment variable that holds the Immich API key")
    p.add_argument("--db-url", help="postgresql://user@host:5432/immich: read the vectors straight from the "
                                    "database (password from PGPASSWORD); without it photos are fetched "
                                    "through the API and the vectors computed locally")
    p.add_argument("--db-url-env", help="name of an environment variable that holds the database address")
    p.add_argument("--model", default=DEFAULT_MODEL, help="Immich's facial recognition model")
    p.add_argument("--max-per-person", type=int, default=MAX_PER_PERSON, help="vectors kept per person")
    p.add_argument("--sample", type=int, default=60, help="API export: photos looked at per person")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("faces-check", help="show what a people file holds")
    p.add_argument("file")
    p.add_argument("--model", default=DEFAULT_MODEL, help="the local recognition model")
    p.set_defaults(func=cmd_check)
