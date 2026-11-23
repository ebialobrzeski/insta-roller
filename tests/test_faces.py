"""Known people: matching, tracking, targeting, geometry and the Immich export.
No network, no insightface: detectors, HTTP and the database are faked."""
import argparse
import builtins
import json
import math

import numpy as np
import pytest

from core import faces
from core.faces import Face, People, Sighting

DIM = 512
RNG = np.random.default_rng(7)


def unit(v):
    return v / np.linalg.norm(v)


def person_vector():
    return unit(RNG.normal(size=DIM))


def near(base, distance):
    """A unit vector at the given cosine distance from `base`."""
    other = RNG.normal(size=DIM)
    other = unit(other - (other @ base) * base)
    cos = 1.0 - distance
    return cos * base + math.sqrt(1 - cos * cos) * other


ANIA, EMIL, STRANGER = person_vector(), person_vector(), person_vector()


def people(model="buffalo_l"):
    return People(model=model, vectors={"Ania": np.array([near(ANIA, 0.1) for _ in range(5)]),
                                        "Emil": np.array([near(EMIL, 0.1) for _ in range(5)])})


# --- file and matching -----------------------------------------------------------
def test_save_load_round_trip(tmp_path):
    path = faces.save_people(tmp_path / "people.json", people().vectors, "buffalo_l", "db", "2.3.1", {"Ania": ["id1"]})
    loaded = faces.load_people(path, model="buffalo_l")
    assert loaded.names == ["Ania", "Emil"] and loaded.immich_version == "2.3.1" and loaded.source == "db"
    assert loaded.vectors["Ania"].shape == (5, DIM)
    assert np.allclose(np.linalg.norm(loaded.vectors["Emil"], axis=1), 1.0, atol=1e-4)
    assert json.loads(path.read_text())["version"] == faces.VERSION


def test_match_threshold():
    p = people()
    name, d = faces.match(near(ANIA, 0.2), p, threshold=0.5)
    assert name == "Ania" and d < 0.5
    name, d = faces.match(STRANGER, p, threshold=0.5)
    assert name is None and d > 0.8
    hard = near(EMIL, 0.5)                               # a cap and sunglasses
    assert faces.match(hard, p, threshold=0.4)[0] is None
    assert faces.match(hard, p, threshold=0.7)[0] == "Emil"
    assert faces.match(ANIA, None) == (None, math.inf)
    assert faces.match(ANIA, People()) == (None, math.inf)


def test_model_mismatch_refused(tmp_path):
    path = faces.save_people(tmp_path / "p.json", people().vectors, "antelopev2", "db")
    with pytest.raises(faces.ModelMismatch):
        faces.load_people(path, model="buffalo_l")
    with pytest.raises(faces.ModelMismatch):
        faces.match(ANIA, faces.load_people(path), model="buffalo_l")
    with pytest.raises(faces.ModelMismatch):
        faces.scan([], people("antelopev2"), FakeDetector({}))
    warnings = []
    assert faces.try_load(path, "buffalo_l", warn=warnings.append) is None and warnings


def test_bad_or_missing_file(tmp_path):
    assert faces.try_load(tmp_path / "none.json") is None
    assert faces.try_load(None) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(faces.FacesError):
        faces.load_people(bad)
    bad.write_text(json.dumps({"format": faces.FORMAT, "version": 99, "people": []}))
    with pytest.raises(faces.FacesError, match="version"):
        faces.load_people(bad)
    assert faces.try_load(bad, warn=lambda m: None) is None


# --- geometry ----------------------------------------------------------------------
def test_geometry_round_trip():
    w, h = 800, 600
    assert faces.view_to_sphere(w / 2, h / 2, w, h, 40, -10, 100) == pytest.approx((40, -10))
    yaw, pitch = faces.view_to_sphere(w * 0.75, h * 0.25, w, h, 0, 0, 100)
    assert yaw > 0 and pitch > 0                         # right of centre and above it
    # the corner of the view lies half the diagonal fov from its centre
    assert faces.angle_between(faces.view_to_sphere(0, 0, w, h, 170, 20, 100), (170, 20)) == pytest.approx(50)
    for view in ((0, 0), (170, 20), (-95, -35)):
        for x, y in ((10, 20), (400, 300), (790, 580), (123, 456)):
            direction = faces.view_to_sphere(x, y, w, h, *view, 100)
            assert -180 <= direction[0] <= 180
            assert faces.sphere_to_view(*direction, w, h, *view, 100) == pytest.approx((x, y), abs=1e-6)
    assert faces.sphere_to_view(180, 0, w, h, 0, 0, 100) is None
    assert faces.angle_between((179, 0), (-179, 0)) == pytest.approx(2)


# --- scanning and tracking -----------------------------------------------------------
class FakeDetector:
    model = "buffalo_l"

    def __init__(self, by_image):
        self.by_image = by_image

    def available(self):
        return True

    def detect(self, image):
        return self.by_image[int(image[0, 0, 0])]


def frame(n):
    image = np.zeros((600, 600, 3), dtype=np.uint8)
    image[0, 0, 0] = n
    return image


def face_at(x, y, vector, score=0.9):
    return Face((x - 30, y - 40, x + 30, y + 40), score, unit(np.asarray(vector, dtype=np.float32)))


def test_scan_tracks_keep_identity_and_strangers_stay_strangers():
    detector = FakeDetector({
        0: [face_at(300, 300, near(ANIA, 0.2)), face_at(100, 300, STRANGER)],
        1: [face_at(320, 300, near(ANIA, 0.65)), face_at(110, 300, STRANGER)],     # Ania put sunglasses on
        2: [face_at(340, 300, near(ANIA, 0.25))],
        3: [face_at(340, 300, STRANGER)],                                           # somebody steps in front
    })
    views = [(float(n), frame(n), 90.0, 0.0, 100.0) for n in range(4)]
    seen = faces.scan(views, people(), detector, threshold=0.5)
    ania = [s for s in seen if s.name == "Ania"]
    assert [s.t for s in ania] == [0.0, 1.0, 2.0]        # the weak middle frame is kept by the track
    assert len({s.track for s in ania}) == 1
    assert ania[0].yaw == pytest.approx(90.0) and ania[0].size > 5
    strangers = [s for s in seen if s.name is None]
    assert len(strangers) == 3 and all(s.name in (None, "Ania") for s in seen)
    # without tracking the middle frame would not have matched
    assert faces.match(detector.by_image[1][0].embedding, people(), 0.5)[0] is None


def test_scan_merges_the_same_face_from_overlapping_views():
    detector = FakeDetector({0: [face_at(300, 300, near(EMIL, 0.2), 0.9)], 1: [face_at(300, 300, near(EMIL, 0.2), 0.8)]})
    seen = faces.scan([(5.0, frame(0), 30.0, 0.0, 100.0), (5.0, frame(1), 32.0, 0.0, 100.0)], people(), detector)
    assert len(seen) == 1 and seen[0].name == "Emil" and seen[0].yaw == pytest.approx(30.0)


def test_degrades_without_insightface_or_people(monkeypatch, tmp_path):
    real_import = builtins.__import__

    def no_insightface(name, *args, **kwargs):
        if name.split(".")[0] in ("insightface", "onnxruntime"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_insightface)
    assert faces.available() is False
    assert faces.scan([(0.0, frame(0), 0.0, 0.0, 100.0)], people()) == []
    assert faces.scan_recording(None, None, [1.0], people(), tmp_path, tmp_path / "log") == []
    with pytest.raises(faces.FacesError, match="insightface"):
        faces.Detector().detect(frame(0))
    with pytest.raises(faces.ExportError, match="insightface"):
        faces.export(["Ania"], tmp_path / "p.json", "http://immich", "key")
    monkeypatch.undo()
    assert faces.scan([(0.0, frame(0), 0.0, 0.0, 100.0)], None, FakeDetector({})) == []
    assert faces.choose_target([], {"Ania": 1.0}, {}) is None
    assert faces.scene_bonus([], 0.2) == 0.0


# --- targeting -----------------------------------------------------------------------
def sight(name, yaw, pitch=0.0, t=0.0, size=10.0):
    return Sighting(t, yaw, pitch, name, size)


def test_strangers_are_never_a_target():
    weights = {"Ania": 1.0, "Emil": 1.0}
    assert faces.choose_target([sight(None, 10), sight(None, 50)], weights, {}) is None
    assert faces.scene_bonus([sight(None, 10)], 0.2, weights) == 0.0
    # a name outside the profile's list is not framed either
    assert faces.choose_target([sight("Zenon", 10)], weights, {}) is None
    target = faces.choose_target([sight(None, 12, size=40), sight("Ania", 100)], weights, {})
    assert target.names == ["Ania"] and target.yaw == pytest.approx(100)
    # a stranger next to a known person does not pull the frame over
    target = faces.choose_target([sight(None, 80), sight("Ania", 100)], weights, {})
    assert target.names == ["Ania"] and target.yaw == pytest.approx(100)
    assert faces.scene_bonus([sight(None, 80), sight("Ania", 100)], 0.2, weights) == 0.2


def test_group_shot_when_known_people_are_close():
    weights = {"Ania": 1.0, "Emil": 1.0}
    single = faces.choose_target([sight("Ania", 100)], weights, {})
    group = faces.choose_target([sight("Ania", 170), sight("Emil", -170)], weights, {"Ania": 100.0})
    assert group.names == ["Ania", "Emil"]
    assert abs(group.yaw) == pytest.approx(180) and group.pitch == pytest.approx(0)
    assert group.fov > single.fov
    apart = faces.choose_target([sight("Ania", 0), sight("Emil", 90)], weights, {})
    assert len(apart.names) == 1


def test_screen_time_balancing_with_weights():
    weights = {"Ania": 2.0, "Emil": 1.0}
    scene = [sight("Ania", 0), sight("Emil", 120)]
    shown: dict[str, float] = {}
    for _ in range(300):
        target = faces.choose_target(scene, weights, shown)
        shown = faces.screen_time(shown, target, 1.0)
    assert shown["Ania"] == pytest.approx(200, abs=2) and shown["Emil"] == pytest.approx(100, abs=2)
    assert faces.choose_target(scene, weights, {"Ania": 50.0, "Emil": 5.0}).names == ["Emil"]
    assert faces.choose_target(scene, weights, {"Ania": 5.0, "Emil": 50.0}).names == ["Ania"]
    assert faces.screen_time({"Ania": 1.0}, None, 5.0) == {"Ania": 1.0}
    # without weights everybody named in the export counts the same
    assert faces.choose_target(scene, None, {"Ania": 9.0}).names == ["Emil"]


# --- export ----------------------------------------------------------------------------
def pg(vector):
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


class FakeCursor:
    """Answers the export's queries from a small in-memory Immich."""

    def __init__(self, schema, persons, vectors, config=None):
        self.schema, self.persons, self.vectors, self.config = schema, persons, vectors, config
        self.queries, self.rows = [], []

    def execute(self, sql, params=()):
        self.queries.append(sql)
        assert sql.lstrip().upper().startswith("SELECT")
        if "information_schema" in sql:
            self.rows = [(t, c) for t, cols in self.schema.items() for c in cols if t in params[0]]
        elif "system_metadata" in sql:
            self.rows = [(json.dumps(self.config),)] if self.config else []
        elif "version_history" in sql:
            self.rows = [("2.3.1",)]
        elif "FROM person" in sql:
            self.rows = [(pid, name) for pid, name in self.persons if name.lower() in params[0]]
        else:
            self.rows = [(pg(v),) for v in self.vectors.get(params[0], [])]

    def fetchall(self):
        return self.rows


NEW = {"person": ["personGroupId", "ownerId", "name"], "asset_face": ["id", "personGroupId", "deletedAt", "isVisible"],
       "face_search": ["faceId", "embedding"], "system_metadata": ["key", "value"],
       "version_history": ["version", "createdAt"]}
OLD = {"person": ["id", "name"], "asset_faces": ["id", "personId"], "face_search": ["faceId", "embedding"]}
PERSONS = [("p-ania", "Ania"), ("p-ania-2", "ania"), ("p-emil", "Emil"), ("p-other", "Zenon")]
VECTORS = {"p-ania": [near(ANIA, 0.1) * 3 for _ in range(80)], "p-ania-2": [near(ANIA, 0.1) for _ in range(4)],
           "p-emil": [near(EMIL, 0.1) for _ in range(6)], "p-other": [STRANGER]}


@pytest.mark.parametrize("schema, table, link", [(NEW, "asset_face", "personGroupId"), (OLD, "asset_faces", "personId")])
def test_db_export_follows_the_schema(schema, table, link):
    cur = FakeCursor(schema, PERSONS, VECTORS)
    vectors, ids, version = faces.export_from_cursor(cur, ["ania", " Emil "], cap=50)
    assert set(vectors) == {"ania", "Emil"} and ids["ania"] == ["p-ania", "p-ania-2"]
    assert vectors["ania"].shape == (50, DIM) and vectors["Emil"].shape == (6, DIM)
    assert np.allclose(np.linalg.norm(vectors["ania"], axis=1), 1.0, atol=1e-5)
    assert version == ("2.3.1" if schema is NEW else None)
    query = cur.queries[-1]
    assert f"FROM {table} f" in query and f'f."{link}"' in query
    assert ('"deletedAt" IS NULL' in query) == (schema is NEW)
    assert faces.match(near(ANIA, 0.2), People(vectors=vectors))[0] == "ania"


def test_db_export_fails_cleanly():
    with pytest.raises(faces.ExportError, match="Nobody"):
        faces.export_from_cursor(FakeCursor(NEW, PERSONS, VECTORS), ["Nobody"])
    with pytest.raises(faces.ExportError, match="schema"):
        faces.export_from_cursor(FakeCursor({"people_v9": ["id"]}, PERSONS, VECTORS), ["Ania"])
    with pytest.raises(faces.ExportError, match="links"):
        faces.export_from_cursor(FakeCursor({"person": ["uuid", "name"], "asset_face": ["id"]}, PERSONS, VECTORS), ["Ania"])
    other = {"machineLearning": {"facialRecognition": {"modelName": "antelopev2"}}}
    with pytest.raises(faces.ModelMismatch):
        faces.export_from_cursor(FakeCursor(NEW, PERSONS, VECTORS, other), ["Ania"])
    faces.export_from_cursor(FakeCursor(NEW, PERSONS, VECTORS, {"machineLearning": {}}), ["Ania"])


def test_failed_export_keeps_the_old_file(tmp_path, monkeypatch):
    path = faces.save_people(tmp_path / "people.json", people().vectors, "buffalo_l", "db")
    before = path.read_text()

    def broken(*args):
        raise faces.ExportError("Immich changed")

    monkeypatch.setattr(faces, "export_db", broken)
    with pytest.raises(faces.ExportError):
        faces.export(["Ania"], path, db_url="postgresql://x")
    assert path.read_text() == before and faces.load_people(path).names == ["Ania", "Emil"]


class FakeImmich:
    """The HTTP layer: images are JSON lists of the faces 'in' them."""

    def __init__(self, new_search=False):
        self.calls, self.new_search = [], new_search

    def __call__(self, method, url, headers, body):
        assert headers["x-api-key"] == "secret"
        path = url.split("/api", 1)[1]
        self.calls.append((method, path))
        if path.startswith("/people?"):
            page = int(path.split("page=")[1])
            everyone = [{"id": "p-ania", "name": "Ania"}, {"id": "p-zenon", "name": "Zenon"}, {"id": "p-emil", "name": "Emil"}]
            return json.dumps({"people": everyone[page - 1:page], "total": 3, "hasNextPage": page < 3}).encode()
        if path == "/server/version":
            return b'{"major": 2, "minor": 3, "patch": 1}'
        if path == "/people/p-ania/thumbnail":
            return b"thumb-ania"
        if path == "/people/p-emil/thumbnail":
            raise faces.urllib.error.HTTPError(url, 404, "Not Found", None, None)
        if path == "/search/metadata":
            query = json.loads(body)
            if self.new_search and "personIds" in query:
                raise faces.urllib.error.HTTPError(url, 400, "Bad Request", None, None)
            pid = (query.get("personIds") or query["filter"]["personIds"]["any"])[0]
            return json.dumps({"assets": {"items": [{"id": f"{pid}-a{i}"} for i in range(3)], "nextPage": None}}).encode()
        if path.startswith("/faces?id="):
            pid = path.split("=")[1].rsplit("-", 1)[0]
            if pid == "p-ania":                          # no boxes: found by likeness to the thumbnail
                return b"[]"
            return json.dumps([{"imageWidth": 2000, "imageHeight": 1000, "boundingBoxX1": 1000, "boundingBoxY1": 400,
                                "boundingBoxX2": 1200, "boundingBoxY2": 600, "person": {"id": pid}}]).encode()
        if path.endswith("/thumbnail?size=preview"):
            return path.split("/")[2].encode()
        raise AssertionError(path)


class Picture:
    shape = (500, 1000, 3)

    def __init__(self, data):
        self.key = data.decode()


class ExportDetector:
    model = "buffalo_l"

    def available(self):
        return True

    def detect(self, image):
        key = image.key
        if key == "thumb-ania":
            return [face_at(50, 50, near(ANIA, 0.05))]
        owner = ANIA if key.startswith("p-ania") else EMIL
        # Immich's box (1000..1200 of 2000 wide) on a 1000 x 500 preview is x 500..600, y 200..300
        return [Face((40, 40, 100, 120), 0.9, unit(STRANGER)), Face((505, 205, 595, 300), 0.9, near(owner, 0.15))]


@pytest.mark.parametrize("new_search", [False, True])
def test_api_export_with_mocked_http(tmp_path, monkeypatch, new_search):
    http = FakeImmich(new_search)
    monkeypatch.setattr(faces, "_decode", Picture)
    client = faces.Immich("http://immich:2283/api/", "secret", fetch=http)
    vectors, ids, version = faces.export_api(client, ["Ania", "Emil"], ExportDetector(), progress=lambda m: None)
    assert version == "2.3.1" and ids == {"Ania": ["p-ania"], "Emil": ["p-emil"]}
    assert len(vectors["Ania"]) == 4 and len(vectors["Emil"]) == 3      # thumbnail + 3 photos; 3 photos
    loaded = People(vectors=vectors)
    assert faces.match(near(ANIA, 0.2), loaded)[0] == "Ania" and faces.match(near(EMIL, 0.2), loaded)[0] == "Emil"
    assert faces.match(STRANGER, loaded)[0] is None
    assert all(method == "GET" or path == "/search/metadata" for method, path in http.calls)
    assert not any("zenon" in path for _, path in http.calls)


def test_api_errors_do_not_leak_the_key():
    def down(method, url, headers, body):
        raise faces.urllib.error.URLError("refused")

    with pytest.raises(faces.ExportError) as e:
        faces.Immich("http://immich:2283", "secret-key", fetch=down).people()
    assert "secret-key" not in str(e.value)
    assert faces.Immich("http://immich:2283", "k", fetch=down).version() is None


# --- commands ------------------------------------------------------------------------
def parser():
    p = argparse.ArgumentParser()
    faces.add_commands(p.add_subparsers(dest="command", required=True))
    return p


def test_commands(tmp_path, capsys, monkeypatch):
    path = faces.save_people(tmp_path / "people.json", people().vectors, "buffalo_l", "db", "2.3.1")
    args = parser().parse_args(["faces-check", str(path)])
    args.func(args)
    out = capsys.readouterr().out
    assert "buffalo_l" in out and "Ania" in out and "5 vectors" in out and "2.3.1" in out

    seen = {}

    def fake_export(names, out, immich_url, api_key, db_url, model, cap, sample):
        seen.update(names=names, key=api_key, db=db_url, model=model)
        return people()

    monkeypatch.setattr(faces, "export", fake_export)
    monkeypatch.setenv("MY_KEY", "from-env")
    args = parser().parse_args(["faces-export", "--immich-url", "http://immich:2283", "--api-key-env", "MY_KEY",
                                "--people", "Ania,Emil", "--out", str(tmp_path / "new.json")])
    args.func(args)
    assert seen == {"names": ["Ania", "Emil"], "key": "from-env", "db": "", "model": "buffalo_l"}
    assert "from-env" not in capsys.readouterr().out

    monkeypatch.delenv("MY_KEY")
    with pytest.raises(SystemExit, match="MY_KEY"):
        args.func(args)
