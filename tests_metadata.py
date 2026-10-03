"""Notes scrubbing and bulk visibility."""

import importlib, json, os, pathlib, sys, tempfile, types

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
WORK = pathlib.Path(tempfile.mkdtemp(prefix="tidalcleanup-meta-"))
os.environ["TIDAL_CLEANUP_DATA"] = str(WORK)

from tidal_cleanup import paths
importlib.reload(paths)
from tidal_cleanup import fetch, actions, metadata, report, cli
for m in (fetch, actions, metadata, report, cli):
    importlib.reload(m)

AD = ("This playlist was created by https://www.tunemymusic.com that lets you "
      "transfer your playlist to Tidal from any music platform")
REAL = "Sunday morning pancakes playlist, curated by me"

STATE = {
    "p1": {"title": "Mixtape 1", "description": AD, "public": False},
    "p2": {"title": "Mixtape 2", "description": AD, "public": True},
    "p3": {"title": "Morning", "description": REAL, "public": True},
    "p4": {"title": "Empty note", "description": "", "public": False},
}
WRITES, VIS = [], []


def get_json(session, path, params=None, base_url=None):
    params = params or {}
    if path.endswith("playlistsAndFavoritePlaylists"):
        items = []
        for pid, st in STATE.items():
            items.append({"created": "c", "playlist": {
                "uuid": pid, "title": st["title"], "description": st["description"],
                "created": "c", "lastUpdated": "u", "numberOfTracks": 2,
                "numberOfVideos": 0, "duration": 400, "type": "USER",
                "publicPlaylist": st["public"], "creator": {"id": 7}}})
        return {"totalNumberOfItems": len(items), "items": items}
    if path.startswith("playlists/") and path.endswith("/items"):
        pid = path.split("/")[1]
        return {"totalNumberOfItems": 2, "items": [
            {"type": "track", "item": {"id": f"{pid}-{i}", "title": f"T{i}",
             "trackNumber": i, "volumeNumber": 1, "duration": 200,
             "artist": {"id": 1, "name": "A"}, "artists": [{"id": 1, "name": "A"}],
             "album": {"id": 9000 + i, "title": f"Alb {i}"}}} for i in (1, 2)]}
    if path.startswith("albums/"):
        raise RuntimeError("404 not found")
    if "/favorites/" in path:
        return {"totalNumberOfItems": 0, "items": []}
    if path == "search":
        return {"albums": {"items": []}}
    raise AssertionError(path)


class Req:
    def request(self, method, path, params=None, data=None, base_url=None):
        if method == "POST" and path.startswith("playlists/"):
            pid = path.split("/")[1]
            assert "title" in data and "description" in data, data
            STATE[pid]["title"] = data["title"]
            STATE[pid]["description"] = data["description"]
            WRITES.append((pid, data["description"]))
            return types.SimpleNamespace(ok=True, status_code=200)
        if method == "PUT" and "/set-" in path:
            assert base_url and base_url.endswith("/v2/"), f"must use v2 host: {base_url}"
            pid = path.split("/")[1]
            want = path.endswith("set-public")
            STATE[pid]["public"] = want
            VIS.append((pid, want))
            return types.SimpleNamespace(ok=True, status_code=200)
        raise AssertionError(f"{method} {path}")


S = types.SimpleNamespace(
    user=types.SimpleNamespace(id=7, username="t",
                               favorites=types.SimpleNamespace(albums=lambda **k: [],
                                                               artists=lambda **k: [])),
    request=Req(),
    config=types.SimpleNamespace(api_v2_location="https://api.tidal.com/v2/"))
fetch.get_json = get_json
fetch.thread_session = lambda p: S
cli.auth = types.SimpleNamespace(login=lambda *a, **k: S, logout=lambda: None)


def check(label, got, expected):
    ok = got == expected
    print(f"{'PASS' if ok else 'FAIL'}  {label:<52} got={got!r} expected={expected!r}")
    return ok


ok = True
cli.main(["audit", "--sample", "0"])
from tidal_cleanup import snapshot
rows = snapshot.load()["playlists"]

print("=== selection ===")
ok &= check("groups identical notes together",
            [n for _, n in metadata.note_groups(rows)], [2, 1])
ok &= check("--match selects only the ad",
            sorted(p["id"] for p in metadata.select_by_note(rows, "tunemymusic", False)),
            ["p1", "p2"])
ok &= check("--match is case-insensitive",
            len(metadata.select_by_note(rows, "TUNEMYMUSIC", False)), 2)
ok &= check("--all skips playlists with no note",
            sorted(p["id"] for p in metadata.select_by_note(rows, None, True)),
            ["p1", "p2", "p3"])

print("\n=== refuses to clear without a selection ===")
ok &= check("exit code 2", cli.main(["notes", "--clear", "--yes"]), 2)
ok &= check("nothing written", WRITES, [])

print("\n=== dry run ===")
cli.main(["notes", "--clear", "--match", "tunemymusic", "--dry-run"])
ok &= check("dry run wrote nothing", WRITES, [])

print("\n=== clear the ad only ===")
rc = cli.main(["notes", "--clear", "--match", "tunemymusic", "--yes"])
ok &= check("exit code", rc, 0)
ok &= check("ad notes cleared", sorted(WRITES), [("p1", ""), ("p2", "")])
ok &= check("curated note untouched", STATE["p3"]["description"], REAL)
ok &= check("titles preserved by the edit", STATE["p1"]["title"], "Mixtape 1")
backups = metadata.list_note_backups()
ok &= check("a backup was written", len(backups), 1)

print("\n=== restore ===")
WRITES.clear()
res = metadata.restore_notes(S, backups[0], verbose=False)
ok &= check("restored both", res, {"restored": 2, "of": 2})
ok &= check("the ad text came back", STATE["p1"]["description"], AD)
ok &= check("snapshot reflected the clearing",
            sorted((p["description"] or "") for p in snapshot.load()["playlists"]),
            ["", "", "", REAL])

print("\n=== visibility ===")
cli.main(["audit", "--refresh", "--sample", "0"])
rows = snapshot.load()["playlists"]
VIS.clear()
ok &= check("exit code", cli.main(["visibility", "--private", "--yes"]), 0)
ok &= check("only the public ones were flipped", sorted(p for p, _ in VIS), ["p2", "p3"])
ok &= check("all now private", all(not s["public"] for s in STATE.values()), True)
ok &= check("snapshot updated so it no longer claims they are public",
            [p["public"] for p in snapshot.load()["playlists"]], [False] * 4)
VIS.clear()
cli.main(["visibility", "--private", "--yes"])
ok &= check("re-running issues no writes", VIS, [])

print("\n=== going public is reported and logged ===")
VIS.clear()
metadata.apply_visibility(S, snapshot.load()["playlists"], public=True, verbose=False)
ok &= check("all flipped public", len(VIS), 4)
log = [json.loads(l) for l in (WORK / "actions.jsonl").read_text().splitlines()]
kinds = sorted({a["action"] for a in log})
ok &= check("actions logged", kinds,
            ["clear_note", "restore_notes", "set_private", "set_public"])
ok &= check("log records the previous visibility",
            all("was_public" in a for a in log if a["action"] == "set_public"), True)

print("\n=== playlists deleted outside the tool ===")
# Reproduces the real failure: the snapshot still lists playlists that have since
# been deleted in the Tidal app, so writing to them returns "Object not found".
cli.main(["audit", "--refresh", "--sample", "0"])
STATE["p1"]["description"] = AD
STATE["p2"]["description"] = AD
cli.main(["audit", "--refresh", "--sample", "0"])
before_snap = len(snapshot.load()["playlists"])
GONE = {"p1", "p2"}
for pid in GONE:
    del STATE[pid]          # deleted elsewhere; the snapshot does not know yet
WRITES.clear()

rc = cli.main(["notes", "--clear", "--all", "--yes"])
ok &= check("exit code is still success", rc, 0)
ok &= check("no write was attempted against a deleted playlist",
            sorted({pid for pid, _ in WRITES} & GONE), [])
ok &= check("the surviving playlist was still cleared",
            STATE["p3"]["description"], "")
ok &= check("snapshot pruned of the deleted playlists",
            {p["id"] for p in snapshot.load()["playlists"]} & GONE, set())
ok &= check("snapshot shrank by exactly the deleted ones",
            before_snap - len(snapshot.load()["playlists"]), 2)

print("\n--- a 404 during the write is reported as 'gone', not a bare error ---")
STATE["p9"] = {"title": "Vanishes mid-run", "description": AD, "public": False}
cli.main(["audit", "--refresh", "--sample", "0"])
real_req = Req.request


def flaky(self, method, path, params=None, data=None, base_url=None):
    if path.startswith("playlists/p9"):
        raise RuntimeError("Object not found")
    return real_req(self, method, path, params=params, data=data, base_url=base_url)


Req.request = flaky
res = metadata.clear_notes(S, [p for p in snapshot.load()["playlists"]
                               if p["id"] == "p9"], verbose=False)
Req.request = real_req
ok &= check("the failure is classified as vanished",
            [f["vanished"] for f in res["failed"]], [True])
ok &= check("a non-404 error is not misread as vanished",
            metadata._is_missing("500 server error"), False)

print("\n" + ("ALL METADATA TESTS PASSED" if ok else "METADATA TESTS FAILED"))
sys.exit(0 if ok else 1)
