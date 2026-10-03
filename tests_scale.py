"""End-to-end test against a simulated 1000-playlist account.

Exercises the things that only break at scale: real pagination (tidalapi's own
helper silently returns only the first 50), tier assignment, batched favouriting,
the staleness guard, and that curated playlists are never touched.
"""

import json
import os
import pathlib
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

WORK = pathlib.Path(tempfile.mkdtemp(prefix="tidalcleanup-scale-"))
os.environ["TIDAL_CLEANUP_DATA"] = str(WORK)

import importlib
from tidal_cleanup import paths
importlib.reload(paths)
from tidal_cleanup import fetch, snapshot, actions, classify, plan, report, cli
for m in (fetch, snapshot, actions, plan, report, cli):
    importlib.reload(m)

USER_ID = 42

# ---------------------------------------------------------------- fake account

class Fake:
    def __init__(self):
        self.playlists = {}
        self.albums = {}
        self.fav_albums = set()
        self.fav_artists = set()
        self.deleted = []
        self.album_titles = {}
        self.calls = {"list": 0, "items": 0, "album": 0, "album_items": 0,
                      "fav_get": 0, "fav_post": 0, "delete": 0}
        self.live_overrides = {}

    def add_album(self, aid, title, artist, artist_id, n, titles=None):
        self.albums[aid] = {
            "id": aid, "title": title, "numberOfTracks": n, "numberOfVolumes": 1,
            "artist": {"id": artist_id, "name": artist},
            "artists": [{"id": artist_id, "name": artist}],
            "releaseDate": "2015-09-25", "type": "ALBUM", "upc": "x",
        }
        self.album_titles[aid] = titles or [f"Song {i}" for i in range(1, n + 1)]

    def add_playlist(self, pid, title, tracks, owner=USER_ID, updated="2024-01-01T00:00:00.000+0000"):
        self.playlists[pid] = {
            "uuid": pid, "title": title, "description": "", "created": updated,
            "lastUpdated": updated, "numberOfTracks": len(tracks), "numberOfVideos": 0,
            "duration": 200 * len(tracks), "type": "USER", "publicPlaylist": False,
            "creator": {"id": owner}, "_tracks": tracks,
        }


def track(tid, title, album_id, album_title, artist, num):
    return {
        "id": tid, "title": title, "trackNumber": num, "volumeNumber": 1,
        "duration": 200, "isrc": f"ISRC{tid}",
        "artist": {"id": 1, "name": artist},
        "artists": [{"id": 1, "name": artist}],
        "album": {"id": album_id, "title": album_title},
    }


FAKE = Fake()

# 850 textbook album imports -> tier A
for i in range(850):
    aid, artist, title = 1000 + i, f"Artist {i}", f"Album {i}"
    FAKE.add_album(aid, title, artist, 5000 + i, 10)
    FAKE.add_playlist(
        f"pA{i}", f"{artist} - {title}",
        [track(i * 100 + n, f"Song {n}", aid, title, artist, n) for n in range(1, 11)],
    )

# 60 album imports renamed by hand -> tier B
for i in range(60):
    aid, artist, title = 2000 + i, f"Band {i}", f"Record {i}"
    FAKE.add_album(aid, title, artist, 6000 + i, 8,
                   titles=[f"Trk {n}" for n in range(1, 9)])
    FAKE.add_playlist(
        f"pB{i}", f"my favourite thing {i}",
        [track(20000 + i * 100 + n, f"Trk {n}", aid, title, artist, n) for n in range(1, 9)],
    )

# 40 album-plus-extras -> tier C (must never be auto-applied)
for i in range(40):
    aid, artist, title = 3000 + i, f"Group {i}", f"LP {i}"
    FAKE.add_album(aid, title, artist, 7000 + i, 10,
                   titles=[f"Cut {n}" for n in range(1, 11)])
    tracks = [track(30000 + i * 100 + n, f"Cut {n}", aid, title, artist, n) for n in range(1, 11)]
    tracks += [track(39000 + i * 10 + k, f"Bonus {k}", 9900 + k, f"Other {k}", "Someone", k)
               for k in range(1, 4)]
    FAKE.add_playlist(f"pC{i}", f"{artist} - {title}", tracks)

# 50 genuinely curated -> tier D
for i in range(50):
    tracks = [track(40000 + i * 100 + n, f"Mix {n}", 8000 + i * 100 + n,
                    f"Misc {n}", f"Various {n}", n) for n in range(1, 21)]
    FAKE.add_playlist(f"pD{i}", f"Summer mix {i}", tracks)

# 12 playlists the user merely follows -> must be filtered out entirely
for i in range(12):
    FAKE.add_playlist(f"pF{i}", f"Someone else's playlist {i}",
                      [track(50000 + i, "x", 8888, "Whatever", "Them", 1)], owner=999)

ORDER = list(FAKE.playlists.keys())

# ---------------------------------------------------------------- fake transport

def fake_get_json(session, path, params=None, base_url=None):
    params = params or {}
    if path.endswith("playlistsAndFavoritePlaylists"):
        FAKE.calls["list"] += 1
        limit, offset = int(params.get("limit", 50)), int(params.get("offset", 0))
        assert limit <= 50, "Tidal caps this endpoint at 50"
        page = ORDER[offset:offset + limit]
        items = []
        for pid in page:
            p = dict(FAKE.playlists[pid])
            p.pop("_tracks")
            p.update(FAKE.live_overrides.get(pid, {}))
            items.append({"created": p["created"], "playlist": p})
        return {"limit": limit, "offset": offset,
                "totalNumberOfItems": len(ORDER), "items": items}

    if path.startswith("playlists/") and path.endswith("/items"):
        FAKE.calls["items"] += 1
        pid = path.split("/")[1]
        tracks = FAKE.playlists[pid]["_tracks"]
        limit, offset = int(params.get("limit", 100)), int(params.get("offset", 0))
        page = tracks[offset:offset + limit]
        return {"totalNumberOfItems": len(tracks),
                "items": [{"type": "track", "item": t} for t in page]}

    if path.startswith("albums/") and path.endswith("/items"):
        FAKE.calls["album_items"] += 1
        aid = int(path.split("/")[1])
        if aid not in FAKE.albums:
            raise RuntimeError("404 not found")
        titles = FAKE.album_titles[aid]
        limit, offset = int(params.get("limit", 100)), int(params.get("offset", 0))
        page = titles[offset:offset + limit]
        return {"totalNumberOfItems": len(titles),
                "items": [{"type": "track", "item": {"id": f"{aid}-{t}", "title": t}}
                          for t in page]}

    if path.startswith("albums/") and not path.endswith("/items"):
        FAKE.calls["album"] += 1
        aid = int(path.split("/")[1])
        if aid not in FAKE.albums:
            raise RuntimeError("404 not found")
        return FAKE.albums[aid]

    if "/favorites/albums" in path:
        FAKE.calls["fav_get"] += 1
        ids = sorted(FAKE.fav_albums)
        limit, offset = int(params.get("limit", 50)), int(params.get("offset", 0))
        page = ids[offset:offset + limit]
        return {"totalNumberOfItems": len(ids),
                "items": [{"item": {"id": i}} for i in page]}

    if "/favorites/artists" in path:
        FAKE.calls["fav_get"] += 1
        ids = sorted(FAKE.fav_artists)
        limit, offset = int(params.get("limit", 50)), int(params.get("offset", 0))
        page = ids[offset:offset + limit]
        return {"totalNumberOfItems": len(ids),
                "items": [{"item": {"id": i}} for i in page]}

    raise AssertionError(f"unexpected GET {path}")


class FakeFavorites:
    def add_album(self, album_id):
        FAKE.calls["fav_post"] += 1
        ids = album_id if isinstance(album_id, list) else [album_id]
        assert len(ids) <= 50
        FAKE.fav_albums.update(str(i) for i in ids)
        return True

    def add_artist(self, artist_id):
        FAKE.calls["fav_post"] += 1
        ids = artist_id if isinstance(artist_id, list) else [artist_id]
        FAKE.fav_artists.update(str(i) for i in ids)
        return True


class FakeRequest:
    def request(self, method, path, params=None, data=None, base_url=None):
        if method == "DELETE" and path.startswith("playlists/"):
            FAKE.calls["delete"] += 1
            pid = path.split("/")[1]
            assert pid in FAKE.playlists, f"deleting unknown playlist {pid}"
            assert FAKE.playlists[pid]["creator"]["id"] == USER_ID, "deleting a followed playlist!"
            FAKE.deleted.append(pid)
            FAKE.playlists.pop(pid)
            return types.SimpleNamespace(ok=True)
        raise AssertionError(f"unexpected {method} {path}")


class FakeSession:
    def __init__(self):
        self.user = types.SimpleNamespace(
            id=USER_ID, username="testuser", favorites=FakeFavorites()
        )
        self.request = FakeRequest()


SESSION = FakeSession()
fetch.get_json = fake_get_json
fetch.thread_session = lambda primary: SESSION
cli.auth = types.SimpleNamespace(login=lambda *a, **k: SESSION, logout=lambda: None)

# ---------------------------------------------------------------- run

def check(label, got, expected):
    ok = got == expected
    print(f"{'PASS' if ok else 'FAIL'}  {label:<48} got={got!r} expected={expected!r}")
    return ok

ok = True
print("=== audit ===")
rc = cli.main(["audit", "--sample", "0"])
entries = classify.annotate(classify.classify_all(snapshot.load()))
counts = report.tier_counts(entries)

ok &= check("playlists seen (not just the first 50)", len(entries), 1000)
ok &= check("followed playlists excluded", len([e for e in entries if e["name"].startswith("Someone")]), 0)
ok &= check("tier A", counts["A"], 850)
ok &= check("tier B", counts["B"], 60)
ok &= check("tier C", counts["C"], 40)
ok &= check("tier D", counts["D"], 50)
ok &= check("listing requests", FAKE.calls["list"], 21)
ok &= check("plan rows", sum(1 for _ in paths.PLAN_FILE.open()) - 1, 1000)

print("\n=== cache reuse: second audit should refetch nothing ===")
before = dict(FAKE.calls)
cli.main(["audit", "--refresh", "--sample", "0"])
ok &= check("track refetches on unchanged account", FAKE.calls["items"] - before["items"], 0)
ok &= check("album refetches", FAKE.calls["album"] - before["album"], 0)
ok &= check("album track-list refetches", FAKE.calls["album_items"] - before["album_items"], 0)
ok &= check("album track lists were fetched at all", FAKE.calls["album_items"] > 900, True)

print("\n=== staleness guard ===")
FAKE.live_overrides["pA7"] = {"lastUpdated": "2026-06-06T00:00:00.000+0000"}

print("\n=== dry run ===")
before_del = len(FAKE.deleted)
cli.main(["apply", "--tier", "A", "--dry-run"])
ok &= check("dry run deleted nothing", len(FAKE.deleted), before_del)
ok &= check("dry run favourited nothing", len(FAKE.fav_albums), 0)

print("\n=== apply tier A ===")
rc = cli.main(["apply", "--tier", "A", "--yes"])
ok &= check("exit code", rc, 0)
ok &= check("playlists deleted", len(FAKE.deleted), 849)   # pA7 held back as stale
ok &= check("stale playlist survived", "pA7" in FAKE.playlists, True)
ok &= check("albums favourited", len(FAKE.fav_albums), 849)
ok &= check("artists followed", len(FAKE.fav_artists), 849)
ok &= check("favourite POSTs were batched", FAKE.calls["fav_post"] <= 40, True)
ok &= check("tier D playlists untouched", all(f"pD{i}" in FAKE.playlists for i in range(50)), True)
ok &= check("tier C playlists untouched", all(f"pC{i}" in FAKE.playlists for i in range(40)), True)
ok &= check("tier B playlists untouched", all(f"pB{i}" in FAKE.playlists for i in range(60)), True)
ok &= check("followed playlists untouched", all(f"pF{i}" in FAKE.playlists for i in range(12)), True)

backups = list(actions.list_backups())
ok &= check("backups written", len(backups), 849)
sample = json.loads(backups[0].read_text())["playlist"]
ok &= check("backup holds the full track list", len(sample["tracks"]) > 0, True)

print("\n=== refusals ===")
ok &= check("refuses tier D", cli.main(["apply", "--tier", "D", "--yes"]), 2)

print("\n=== restore round-trip ===")
created = {}
def fake_create(name, desc=""):
    pid = f"restored-{len(created)}"
    created[pid] = {"name": name, "tracks": []}
    return types.SimpleNamespace(id=pid, add=lambda ids: created[pid]["tracks"].extend(ids))
SESSION.user.create_playlist = fake_create
res = actions.restore(SESSION, backups[0])
ok &= check("restored track count matches backup", res["tracks_added"], len(sample["tracks"]))

print("\n=== request budget ===")
print(f"  total simulated requests: {sum(FAKE.calls.values())}  {FAKE.calls}")

print("\n" + ("ALL SCALE TESTS PASSED" if ok else "SCALE TESTS FAILED"))
sys.exit(0 if ok else 1)
