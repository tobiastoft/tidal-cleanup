"""Triage loop: scripted keypresses, deferred deletion, undo, and the keep->ignore link."""

import importlib
import json
import os
import pathlib
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
WORK = pathlib.Path(tempfile.mkdtemp(prefix="tidalcleanup-triage-"))
os.environ["TIDAL_CLEANUP_DATA"] = str(WORK)

from tidal_cleanup import paths
importlib.reload(paths)
from tidal_cleanup import fetch, snapshot, actions, classify, plan, report, keys, triage, cli
for m in (fetch, snapshot, actions, plan, report, keys, triage, cli):
    importlib.reload(m)

USER = 7
DELETED, FAV_A, FAV_R = [], set(), set()


def track(t, aid, title, artist, n):
    return {"id": str(t), "title": title, "trackNumber": n, "volumeNumber": 1,
            "duration": 200, "isrc": str(t), "artist": {"id": 1, "name": artist},
            "artists": [{"id": 1, "name": artist}], "album": {"id": aid, "title": "Rec"}}


PLS, ALBUMS, ALBUM_TITLES = {}, {}, {}
# p0..p3 curated; p4 is a real album copy (so [a] is offered)
for i in range(4):
    PLS[f"p{i}"] = {
        "uuid": f"p{i}", "title": f"Mixtape {i}", "description": "", "created": "2020-01-01",
        "lastUpdated": "u", "numberOfTracks": 5, "numberOfVideos": 0, "duration": 1000,
        "type": "USER", "publicPlaylist": False, "creator": {"id": USER},
        "_tracks": [track(i * 10 + n, 9000 + i * 10 + n, f"Song {i}-{n}", f"Artist {n}", n)
                    for n in range(1, 6)]}
ALBUMS[500] = {"id": 500, "title": "Rec", "numberOfTracks": 4, "numberOfVolumes": 1,
               "artist": {"id": 90, "name": "A"}, "artists": [{"id": 90, "name": "A"}],
               "releaseDate": "2020-01-01", "type": "ALBUM", "upc": "u"}
ALBUM_TITLES[500] = [f"Trk {n}" for n in range(1, 5)]
PLS["p4"] = {"uuid": "p4", "title": "A - Rec", "description": "", "created": "2021-01-01",
             "lastUpdated": "u", "numberOfTracks": 4, "numberOfVideos": 0, "duration": 800,
             "type": "USER", "publicPlaylist": False, "creator": {"id": USER},
             "_tracks": [track(400 + n, 500, f"Trk {n}", "A", n) for n in range(1, 5)]}


def get_json(session, path, params=None, base_url=None):
    params = params or {}
    if path.endswith("playlistsAndFavoritePlaylists"):
        items = []
        for pid, p in PLS.items():
            q = dict(p); q.pop("_tracks"); items.append({"created": q["created"], "playlist": q})
        return {"totalNumberOfItems": len(items), "items": items}
    if path.startswith("playlists/") and path.endswith("/items"):
        pid = path.split("/")[1]
        return {"totalNumberOfItems": len(PLS[pid]["_tracks"]),
                "items": [{"type": "track", "item": t} for t in PLS[pid]["_tracks"]]}
    if path.startswith("albums/") and path.endswith("/items"):
        aid = int(path.split("/")[1])
        if aid not in ALBUM_TITLES: raise RuntimeError("404 not found")
        return {"totalNumberOfItems": len(ALBUM_TITLES[aid]),
                "items": [{"item": {"id": f"{aid}-{t}", "title": t}} for t in ALBUM_TITLES[aid]]}
    if path.startswith("albums/"):
        aid = int(path.split("/")[1])
        if aid not in ALBUMS: raise RuntimeError("404 not found")
        return ALBUMS[aid]
    if "/favorites/albums" in path:
        ids = sorted(FAV_A)
        return {"totalNumberOfItems": len(ids), "items": [{"item": {"id": i}} for i in ids]}
    if "/favorites/artists" in path:
        ids = sorted(FAV_R)
        return {"totalNumberOfItems": len(ids), "items": [{"item": {"id": i}} for i in ids]}
    if path == "search":
        return {"albums": {"items": []}}
    raise AssertionError(path)


class Fav:
    def add_album(self, x): FAV_A.update(str(i) for i in (x if isinstance(x, list) else [x]))
    def add_artist(self, x): FAV_R.update(str(i) for i in (x if isinstance(x, list) else [x]))


class Req:
    def request(self, method, path, params=None, data=None, base_url=None):
        assert method == "DELETE", method
        pid = path.split("/")[1]
        DELETED.append(pid); PLS.pop(pid)
        return types.SimpleNamespace(ok=True)


S = types.SimpleNamespace(
    user=types.SimpleNamespace(id=USER, username="t", favorites=Fav()), request=Req())
fetch.get_json = get_json
fetch.thread_session = lambda p: S
cli.auth = types.SimpleNamespace(login=lambda *a, **k: S, logout=lambda: None)

SCRIPT = iter([])
keys.interactive = lambda: True
keys.read_key = lambda: next(SCRIPT)
triage.keys = keys


def script(*presses):
    global SCRIPT
    SCRIPT = iter(list(presses) + ["q"] * 50)


def check(label, got, expected):
    ok = got == expected
    print(f"{'PASS' if ok else 'FAIL'}  {label:<52} got={got!r} expected={expected!r}")
    return ok


ok = True
cli.main(["audit", "--sample", "0"])

print("=== loop marks decisions but deletes nothing ===")
# name order puts "A - Rec" first, then Mixtape 0..3
script("a", "d", "k", "s", "d")
cli.main(["triage", "--order", "name", "--dry-run"])
d = triage.load_decisions()
ok &= check("decisions recorded", len(d), 4)
ok &= check("nothing deleted during the loop", DELETED, [])
ok &= check("Mixtape 0 marked delete", d["p0"]["decision"], "delete")
ok &= check("Mixtape 1 marked keep", d["p1"]["decision"], "keep")
ok &= check("Mixtape 2 left undecided", "p2" in d, False)
ok &= check("album offered convert", d["p4"]["decision"], "convert")
ok &= check("keep also excludes it from album conversion",
            "p1" in actions.load_ignored(), True)

print("\n=== undo ===")
script("d", "u")
cli.main(["triage", "--order", "name", "--redo", "--dry-run"])
ok &= check("undo removed the decision", triage.load_decisions(), {})

print("\n=== decisions survive a quit, and resume skips decided ones ===")
script("d", "q")
cli.main(["triage", "--order", "name", "--redo", "--dry-run"])
first = triage.load_decisions()
ok &= check("one decision saved before quitting", len(first), 1)
script("d")
cli.main(["triage", "--order", "name", "--dry-run"])
after = triage.load_decisions()
ok &= check("resumed onto the next undecided playlist", len(after), 2)
ok &= check("earlier decision untouched", list(first)[0] in after, True)

print("\n=== commit applies them ===")
triage.save_decisions({
    "p0": {"decision": "delete", "name": "Mixtape 0", "num_tracks": 5, "at": "x"},
    "p3": {"decision": "delete", "name": "Mixtape 3", "num_tracks": 5, "at": "x"},
    "p1": {"decision": "keep", "name": "Mixtape 1", "at": "x"},
    "p4": {"decision": "convert", "name": "A - Rec", "album_id": "500", "at": "x"},
})
rc = cli.main(["triage", "--commit", "--yes"])
ok &= check("exit code", rc, 0)
ok &= check("deleted exactly the marked ones", sorted(DELETED), ["p0", "p3", "p4"])
ok &= check("kept playlist survives", "p1" in PLS, True)
ok &= check("undecided playlist survives", "p2" in PLS, True)
ok &= check("album was saved before its playlist went", FAV_A, {"500"})
backups = actions.list_backups()
ok &= check("every deletion was backed up first", len(backups), 3)
names = {json.loads(b.read_text())["playlist"]["name"] for b in backups}
ok &= check("backups hold the right playlists",
            names, {"Mixtape 0", "Mixtape 3", "A - Rec"})
sample = json.loads([b for b in backups if "Mixtape 0" in
                     json.loads(b.read_text())["playlist"]["name"]][0].read_text())
ok &= check("backup holds full track list", len(sample["playlist"]["tracks"]), 5)
ok &= check("applied decisions cleared", set(triage.load_decisions()), {"p1"})

print("\n=== restore brings a triaged playlist back ===")
created = {}
def fake_create(name, desc=""):
    pid = f"restored-{len(created)}"
    created[pid] = []
    return types.SimpleNamespace(id=pid, add=lambda ids: created[pid].extend(ids))
S.user.create_playlist = fake_create
target = [b for b in backups if "Mixtape 3" in json.loads(b.read_text())["playlist"]["name"]][0]
res = actions.restore(S, target)
ok &= check("restored all 5 tracks", res["tracks_added"], 5)

print("\n" + ("ALL TRIAGE TESTS PASSED" if ok else "TRIAGE TESTS FAILED"))
sys.exit(0 if ok else 1)
