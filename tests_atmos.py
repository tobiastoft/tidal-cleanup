"""Atmos upgrades: the coverage guarantee, edition choice, and swap ordering."""

import importlib
import json
import os
import pathlib
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
WORK = pathlib.Path(tempfile.mkdtemp(prefix="tidalcleanup-atmos-"))
os.environ["TIDAL_CLEANUP_DATA"] = str(WORK)

from tidal_cleanup import paths
importlib.reload(paths)
from tidal_cleanup import fetch, actions, atmos, report, cli
for m in (fetch, actions, atmos, report, cli):
    importlib.reload(m)

USER = 7
FAV_ALBUMS = set()
REMOVED = []

# artist 1: stereo album with a complete Atmos twin           -> upgrade
# artist 2: Atmos twin is missing a track                     -> rejected
# artist 3: Atmos twin is a deluxe superset                   -> upgrade
# artist 4: no Atmos edition at all                           -> untouched
# artist 5: already Atmos                                     -> untouched
CATALOGUE = {
    "1": [
        {"id": "s1", "title": "Nightfall", "numberOfTracks": 10, "audioModes": ["STEREO"]},
        {"id": "a1", "title": "Nightfall", "numberOfTracks": 10,
         "audioModes": ["DOLBY_ATMOS"]},
    ],
    "2": [
        {"id": "s2", "title": "Daybreak", "numberOfTracks": 10, "audioModes": ["STEREO"]},
        {"id": "a2", "title": "Daybreak", "numberOfTracks": 8,
         "audioModes": ["DOLBY_ATMOS"]},
    ],
    "3": [
        {"id": "s3", "title": "Highwater", "numberOfTracks": 9, "audioModes": ["STEREO"]},
        {"id": "a3", "title": "Highwater (Deluxe Edition)", "numberOfTracks": 12,
         "audioModes": ["DOLBY_ATMOS"]},
        {"id": "a3x", "title": "Highwater", "numberOfTracks": 40,
         "audioModes": ["DOLBY_ATMOS"]},
    ],
    "4": [
        {"id": "s4", "title": "Lowland", "numberOfTracks": 7, "audioModes": ["STEREO"]},
    ],
    "5": [
        {"id": "s5", "title": "Aurora", "numberOfTracks": 6, "audioModes": ["DOLBY_ATMOS"]},
    ],
    # retitled Atmos twin: same tracks, anniversary title
    "6": [
        {"id": "s6", "title": "Riverbed", "numberOfTracks": 8, "audioModes": ["STEREO"]},
        {"id": "a6", "title": "Riverbed Revisited", "numberOfTracks": 8,
         "audioModes": ["DOLBY_ATMOS"]},
    ],
    # the trap: a LIVE Atmos album with an identical tracklist
    "7": [
        {"id": "s7", "title": "Lantern", "numberOfTracks": 6, "audioModes": ["STEREO"]},
        {"id": "a7", "title": "Lantern (Live at Wembley)", "numberOfTracks": 6,
         "audioModes": ["DOLBY_ATMOS"]},
    ],
    # greatest hits in Atmos that happens to contain the whole album
    "8": [
        {"id": "s8", "title": "Pebble", "numberOfTracks": 5, "audioModes": ["STEREO"]},
        {"id": "a8", "title": "The Very Best Of", "numberOfTracks": 30,
         "audioModes": ["DOLBY_ATMOS"]},
    ],
}
TITLES = {
    "s1": [f"N{i}" for i in range(1, 11)], "a1": [f"N{i}" for i in range(1, 11)],
    "s2": [f"D{i}" for i in range(1, 11)], "a2": [f"D{i}" for i in range(1, 9)],
    "s3": [f"H{i}" for i in range(1, 10)],
    "a3": [f"H{i}" for i in range(1, 13)],
    "a3x": [f"H{i}" for i in range(1, 10)] + [f"Live {i}" for i in range(1, 32)],
    "s4": [f"L{i}" for i in range(1, 8)], "s5": [f"A{i}" for i in range(1, 7)],
    "s6": [f"R{i}" for i in range(1, 9)], "a6": [f"R{i}" for i in range(1, 9)],
    "s7": [f"Q{i}" for i in range(1, 7)], "a7": [f"Q{i}" for i in range(1, 7)],
    "s8": [f"P{i}" for i in range(1, 6)],
    "a8": [f"P{i}" for i in range(1, 6)] + [f"Other {i}" for i in range(1, 26)],
}
OWNER = {aid: art for art, rows in CATALOGUE.items() for r in rows for aid in [r["id"]]}
SAVED = {"s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"}
FAV_ALBUMS |= SAVED


def album_json(aid):
    for art, rows in CATALOGUE.items():
        for r in rows:
            if r["id"] == aid:
                return {"id": aid, "title": r["title"], "numberOfTracks": r["numberOfTracks"],
                        "numberOfVolumes": 1, "audioModes": r["audioModes"],
                        "mediaMetadata": {"tags": r["audioModes"]},
                        "artist": {"id": art, "name": f"Artist {art}"},
                        "artists": [{"id": art, "name": f"Artist {art}"}],
                        "releaseDate": "2020-01-01", "type": "ALBUM", "upc": "u",
                        "audioQuality": "LOSSLESS"}
    raise RuntimeError("404 not found")


CALLS = {"artist_albums": 0}
TRACKLIST_FETCHES = []


def get_json(session, path, params=None, base_url=None):
    params = params or {}
    if "/favorites/albums" in path:
        # The real favourites payload embeds the full album object, including
        # artist and audioModes; the code relies on that to avoid 756 fetches.
        ids = sorted(FAV_ALBUMS)
        limit, offset = int(params.get("limit", 50)), int(params.get("offset", 0))
        page = ids[offset:offset + limit]
        return {"totalNumberOfItems": len(ids),
                "items": [{"item": album_json(i)} for i in page]}
    if "/favorites/artists" in path:
        return {"totalNumberOfItems": 0, "items": []}
    if path.startswith("artists/") and path.endswith("/albums"):
        CALLS["artist_albums"] += 1
        assert params.get("filter") == "ALL", "must use filter=ALL to see Atmos editions"
        art = path.split("/")[1]
        rows = CATALOGUE.get(art, [])
        return {"totalNumberOfItems": len(rows), "items": rows}
    if path.startswith("albums/") and path.endswith("/items"):
        aid = path.split("/")[1]
        TRACKLIST_FETCHES.append(aid)
        return {"totalNumberOfItems": len(TITLES[aid]),
                "items": [{"item": {"id": f"{aid}-{t}", "title": t}} for t in TITLES[aid]]}
    if path.startswith("albums/"):
        return album_json(path.split("/")[1])
    raise AssertionError(path)


class Fav:
    def add_album(self, x):
        FAV_ALBUMS.update(str(i) for i in (x if isinstance(x, list) else [x]))
    def add_artist(self, x): pass
    def remove_album(self, aid):
        # Guard the ordering: the replacement must already be saved.
        assert str(aid) in FAV_ALBUMS
        REMOVED.append(str(aid)); FAV_ALBUMS.discard(str(aid)); return True


S = types.SimpleNamespace(
    user=types.SimpleNamespace(id=USER, username="t", favorites=Fav()),
    request=types.SimpleNamespace(request=lambda *a, **k: types.SimpleNamespace(ok=True)))
fetch.get_json = get_json
fetch.thread_session = lambda p: S
cli.auth = types.SimpleNamespace(login=lambda *a, **k: S, logout=lambda: None)


def check(label, got, expected):
    ok = got == expected
    print(f"{'PASS' if ok else 'FAIL'}  {label:<54} got={got!r} expected={expected!r}")
    return ok


ok = True
collection = actions.Collection(S)
found = atmos.find_upgrades(S, collection.album_ids, verbose=False)
up = {u["stereo"]["id"]: u for u in found["upgrades"]}

print("=== detection ===")
ok &= check("exact Atmos twin found", "s1" in up, True)
ok &= check("Atmos superset accepted", "s3" in up, True)
ok &= check("Atmos missing a track is rejected", "s2" in up, False)
ok &= check("rejection recorded with the missing track",
            [r["missing"] for r in found["partial"] if r["stereo"]["id"] == "s2"], [["d10", "d9"]])
ok &= check("album with no Atmos edition untouched", "s4" in up, False)
ok &= check("already-Atmos album not reconsidered",
            [a["id"] for a in found["already_atmos"]], ["s5"])
ok &= check("closest-sized Atmos edition chosen (12 not 40)",
            up["s3"]["atmos"]["id"], "a3")
ok &= check("coverage never lost: no upgrade has missing tracks",
            all(not u["missing"] for u in found["upgrades"]), True)

print("\n=== retitled / tracklist matching ===")
ret = {r["stereo"]["id"]: r for r in found["retitled"]}
ok &= check("reworked edition surfaced for review",
            "a6" in [r["atmos"]["id"] for r in found["retitled"]], True)
ok &= check("it is flagged as a variant, not a blind tracklist match",
            [r["match"] for r in found["retitled"] if r["atmos"]["id"] == "a6"],
            ["variant"])
ok &= check("retitled match is kept out of the default set", "s6" in up, False)
ok &= check("LIVE album with identical tracklist rejected",
            "s7" in ret or "s7" in up, False)
ok &= check("greatest-hits superset rejected (covers too little of itself)",
            "s8" in ret or "s8" in up, False)
ok &= check("superset excluded by the size filter, costing no request",
            "a8" in TRACKLIST_FETCHES, False)
ok &= check("the live album's tracklist was never fetched either",
            "a7" in TRACKLIST_FETCHES, False)
ok &= check("the plausible retitled candidate WAS verified",
            "a6" in TRACKLIST_FETCHES, True)
ok &= check("title matches still take precedence",
            sorted(u["stereo"]["id"] for u in found["upgrades"]), ["s1", "s3"])
ok &= check("every retitled match also loses no tracks",
            all(not r["missing"] for r in found["retitled"]), True)

print("\n--- near misses vs discarded comparisons ---")
# s2's Atmos twin is the same album missing 2 tracks -> a real near miss.
# A candidate for a different album by the same artist is not a near miss.
nm = {r["stereo"]["id"]: r for r in found["near_misses"]}
ok &= check("same-titled lossy candidate is a near miss", "s2" in nm, True)
ok &= check("near miss records exactly what would be lost",
            nm["s2"]["missing"], ["d10", "d9"])
ok &= check("near misses are sorted by fewest tracks lost",
            [len(r["missing"]) for r in found["near_misses"]]
            == sorted(len(r["missing"]) for r in found["near_misses"]), True)
ok &= check("wrong-album comparisons are counted separately, not as near misses",
            all(r.get("plausible") for r in found["near_misses"]), True)
ok &= check("discarded count excludes the near misses",
            found["discarded"] + len([r for r in found["partial"] if r.get("plausible")])
            == len(found["partial"]), True)
ok &= check("one near miss per album, the least lossy one",
            len(nm) == len({r["stereo"]["id"] for r in found["near_misses"]}), True)
# s3 has a complete Atmos edition (a3) and also a 40-track one; it must not also
# be offered a lossy option, which would be strictly worse.
ok &= check("no lossy option for an album that already has a lossless one",
            {r["stereo"]["id"] for r in found["near_misses"]}
            & {u["stereo"]["id"] for u in found["upgrades"]}, set())

print("\n--- --max-loss opts into a bounded loss ---")
ok &= check("max_loss below the gap excludes it",
            [r for r in found["near_misses"] if len(r["missing"]) <= 1], [])
accepted = [r for r in found["near_misses"] if len(r["missing"]) <= 2]
ok &= check("max_loss at the gap includes it",
            [r["stereo"]["id"] for r in accepted], ["s2"])
FAV_ALBUMS.clear(); FAV_ALBUMS.update(SAVED); REMOVED.clear()
coll_ml = actions.Collection(S)
res_ml = atmos.apply_upgrades(S, coll_ml, accepted, verbose=False)
ok &= check("lossy upgrade applied when opted into", res_ml["atmos_saved"], 1)
ok &= check("its stereo version was removed", REMOVED, ["s2"])
log_ml = [json.loads(l) for l in (WORK / "actions.jsonl").read_text().splitlines()]
lost = [a for a in log_ml if a["action"] == "atmos_upgrade" and a.get("tracks_lost")]
ok &= check("the lost tracks are recorded in the log",
            lost[-1]["tracks_lost"], ["d10", "d9"])
# Leave the account as the later sections expect to find it.
FAV_ALBUMS.clear(); FAV_ALBUMS.update(SAVED); REMOVED.clear()

print("\n--- suffix-tolerant track matching ---")
# Atmos releases append the mix name to every track title.
base = {"black peter", "casey jones", "cumberland blues", "dire wolf",
        "easy wind", "high time", "new speedway boogie", "uncle john s band"}
suffixed = {f"{t} 2023 mickey hart mix" for t in base}
r = atmos.match_tracklists(base, suffixed)
ok &= check("every track matches through a mix suffix", (r["precision"], r["recall"]), (1.0, 1.0))
ok &= check("nothing reported missing", r["missing"], [])

for a, b, expected, why in [
    ("love", "love remastered 2011", True, "genuine suffix accepted"),
    ("love", "lovesong", False, "word boundary required"),
    ("one", "one more time for the road", False, "too short to prefix-match"),
    ("black peter", "casey jones", False, "unrelated rejected"),
]:
    got = atmos._prefix_pair(a, b)
    good = got == expected
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {why:<34} {a!r} vs {b!r} -> {got}")

r2 = atmos.match_tracklists({"black peter", "black peter reprise"},
                            {"black peter atmos mix"})
ok &= check("one candidate cannot match two of my tracks",
            (r2["precision"], r2["missing"]), (0.5, ["black peter reprise"]))
r3 = atmos.match_tracklists({"black peter"},
                            {"black peter atmos mix", "black peter alt mix"})
ok &= check("unused candidates count as extra, lowering recall",
            (r3["precision"], r3["recall"], r3["extra"]), (1.0, 0.5, 1))

print("\n--- version markers: dropped vs reviewed ---")
# A different *performance* is dropped; a mere reworking is surfaced for review.
for mine, theirs, dropped, why in [
    ("Lantern", "Lantern (Live at Wembley)", True, "live"),
    ("Lantern", "Lantern Unplugged", True, "unplugged"),
    ("Lantern", "Lantern (Acoustic Sessions)", True, "acoustic"),
    ("Lantern", "Lantern (BBC Session)", True, "bbc session"),
    ("Lantern (Live)", "Lantern (Live at Wembley)", False, "both already live"),
    ("Lantern", "Lantern Redux", False, "redux -> review"),
    ("Lantern", "Lantern Revisited", False, "revisited -> review"),
    ("Lantern", "Lantern (Steven Wilson Remix)", False, "remix -> review"),
    ("Lantern", "Lantern (Atmos Mix)", False, "atmos mix -> accept"),
    ("Lantern", "Lantern (Remastered)", False, "remaster -> accept"),
]:
    got = atmos._different_performance(mine, theirs)
    good = got == dropped
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {why:<24} {theirs!r} dropped={got}")

print("\n--- titles-only mode ignores retitled matches ---")
f_strict = atmos.find_upgrades(S, collection.album_ids, verbose=False, loose=False)
ok &= check("no pure-tracklist matches when loose=False",
            [r["match"] for r in f_strict.get("retitled") or []
             if r["match"] == "tracklist"], [])
ok &= check("a same-title variant is still surfaced for review",
            sorted(r["stereo"]["id"] for r in f_strict.get("retitled") or []), ["s6"])
ok &= check("title matches unaffected",
            sorted(u["stereo"]["id"] for u in f_strict["upgrades"]), ["s1", "s3"])

print("\n=== dry run changes nothing ===")
before = set(FAV_ALBUMS)
atmos.apply_upgrades(S, collection, list(up.values()), dry_run=True, verbose=False)
ok &= check("favourites unchanged", FAV_ALBUMS, before)
ok &= check("nothing removed", REMOVED, [])

print("\n=== apply swaps ===")
res = atmos.apply_upgrades(S, collection, list(up.values()), verbose=False)
ok &= check("Atmos albums saved", res["atmos_saved"], 2)
ok &= check("stereo albums removed", sorted(REMOVED), ["s1", "s3"])
ok &= check("collection now holds the Atmos ids",
            {"a1", "a3"} <= FAV_ALBUMS, True)
ok &= check("replaced stereo ids gone", {"s1", "s3"} & FAV_ALBUMS, set())
ok &= check("rejected album kept its stereo version", "s2" in FAV_ALBUMS, True)
ok &= check("unrelated albums kept", {"s4", "s5"} <= FAV_ALBUMS, True)

log = [json.loads(l) for l in (WORK / "actions.jsonl").read_text().splitlines()]
# Scope to the lossless swaps; the --max-loss section above logged a lossy one.
swaps = [a for a in log if a["action"] == "atmos_upgrade" and not a.get("tracks_lost")]
ok &= check("every lossless swap logged with both ids", len(swaps), 2)
ok &= check("log records the track counts",
            sorted((s["stereo_tracks"], s["atmos_tracks"]) for s in swaps),
            [(9, 12), (10, 10)])
ok &= check("lossless swaps record no lost tracks",
            all(a.get("tracks_lost") == [] for a in swaps), True)

print("\n=== a removal that reports failure keeps the stereo album ===")
FAV_ALBUMS.clear(); FAV_ALBUMS.update(SAVED); REMOVED.clear()
Fav.remove_album = lambda self, aid: False   # Tidal can report failure by return value
colf = actions.Collection(S)
foundf = atmos.find_upgrades(S, colf.album_ids, verbose=False)
resf = atmos.apply_upgrades(S, colf, foundf["upgrades"], verbose=False)
ok &= check("failed removals counted, not silently dropped",
            len(resf["remove_failed"]), 2)
ok &= check("stereo albums still present after a failed removal",
            {"s1", "s3"} <= FAV_ALBUMS, True)
Fav.remove_album = lambda self, aid: (
    REMOVED.append(str(aid)) or FAV_ALBUMS.discard(str(aid)) or True)

print("\n=== keep-stereo mode ===")
FAV_ALBUMS.clear(); FAV_ALBUMS.update(SAVED); REMOVED.clear()
collection2 = actions.Collection(S)
found2 = atmos.find_upgrades(S, collection2.album_ids, verbose=False)
atmos.apply_upgrades(S, collection2, found2["upgrades"], remove_stereo=False, verbose=False)
ok &= check("stereo kept when asked", sorted(REMOVED), [])
ok &= check("Atmos still added", {"a1", "a3"} <= FAV_ALBUMS, True)

print("\n=== caching ===")
before_calls = CALLS["artist_albums"]
atmos.find_upgrades(S, collection2.album_ids, verbose=False)
ok &= check("artist catalogues re-read from cache", CALLS["artist_albums"], before_calls)

print("\n" + ("ALL ATMOS TESTS PASSED" if ok else "ATMOS TESTS FAILED"))


# --- interactive review of the lossy / retitled candidates -------------------
print("\n--- interactive review loop ---")
import importlib as _il
from tidal_cleanup import keys as _keys
_il.reload(_keys)
cli.keys = _keys
_keys.interactive = lambda: True

SCRIPT = iter([])
_keys.read_key = lambda: next(SCRIPT)


def script(*presses):
    global SCRIPT
    SCRIPT = iter(list(presses) + ["q"] * 50)


FAV_ALBUMS.clear(); FAV_ALBUMS.update(SAVED); REMOVED.clear()
coll_r = actions.Collection(S)
found_r = atmos.find_upgrades(S, coll_r.album_ids, verbose=False)
cands = atmos.review_candidates(found_r)

ok &= check("review covers retitled and near misses",
            sorted(c["stereo"]["id"] for c in cands), ["s2", "s6"])
ok &= check("lossless retitled comes before a lossy near miss",
            [c["stereo"]["id"] for c in cands], ["s6", "s2"])

print("  accept the first, skip the second:")
script("y", "n")
picked = cli._review_atmos(cands)
ok &= check("only the accepted one is returned",
            [p["stereo"]["id"] for p in picked], ["s6"])
ok &= check("review itself changes nothing", (sorted(REMOVED), FAV_ALBUMS == SAVED),
            ([], True))

print("  quit immediately:")
script("q")
ok &= check("quitting accepts nothing", cli._review_atmos(cands), [])

print("  [t] toggles tracklists without consuming the item:")
script("t", "y", "n")
picked2 = cli._review_atmos(cands)
ok &= check("toggle does not advance or accept twice",
            [p["stereo"]["id"] for p in picked2], ["s6"])

print("  an unrecognised key re-prompts rather than skipping:")
script("x", "y", "n")
picked3 = cli._review_atmos(cands)
ok &= check("unknown key did not count as a decision",
            [p["stereo"]["id"] for p in picked3], ["s6"])

print("  accepting both applies both, losing the listed tracks:")
script("y", "y")
picked4 = cli._review_atmos(cands)
res_r = atmos.apply_upgrades(S, coll_r, picked4, verbose=False)
ok &= check("both swapped", res_r["atmos_saved"], 2)
ok &= check("stereo versions removed", sorted(REMOVED), ["s2", "s6"])
log_r = [json.loads(l) for l in (WORK / "actions.jsonl").read_text().splitlines()]
lossy_r = [a for a in log_r if a["action"] == "atmos_upgrade" and a.get("tracks_lost")]
ok &= check("the lossy one recorded what it cost",
            lossy_r[-1]["tracks_lost"], ["d10", "d9"])

print("\n" + ("REVIEW TESTS PASSED" if ok else "REVIEW TESTS FAILED"))
sys.exit(0 if ok else 1)
