"""Classifier scenarios, including the real-world patterns found in a live library."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from tidal_cleanup.classify import annotate, classify, name_similarity, normalize


def tr(i, name, album_id, album_name, artist, num=None):
    return {"id": str(i), "name": name, "artist": artist, "artists": [artist],
            "album_id": album_id, "album_name": album_name, "album_artist": artist,
            "track_num": num if num is not None else i, "volume_num": 1,
            "isrc": None, "duration": 200}


def pl(pid, name, tracks, desc=""):
    return {"id": pid, "name": name, "description": desc, "num_tracks": len(tracks),
            "num_videos": 0, "tracks": tracks, "share_url": None}


def album(aid, name, artist, titles, num_tracks=None):
    ALBUMS[aid] = {"id": aid, "name": name, "artist": artist, "artist_id": f"ar{aid}",
                   "num_tracks": num_tracks or len(titles)}
    TRACKS[aid] = titles


ALBUMS, TRACKS = {}, {}

album("A1", "b'lieve i'm goin down...", "Kurt Vile", [f"Song {i}" for i in range(1, 13)])
album("A2", "Kid A", "Radiohead", [f"Song {i}" for i in range(1, 11)])
album("A3", "Blonde", "Frank Ocean", [f"Song {i}" for i in range(1, 18)])
album("A4", "Sign o' the Times", "Prince", [f"Song {i}" for i in range(1, 17)])
album("A5", "Abbey Road", "The Beatles", [f"Song {i}" for i in range(1, 18)])
album("A5b", "Abbey Road (Remastered)", "The Beatles", [f"Song {i}" for i in range(1, 18)])
album("A6", "Lonerism", "Tame Impala", [f"Song {i}" for i in range(1, 13)])
album("COMP", "Workout 2019", "Various", [f"Song {i}" for i in range(1, 13)] + [f"Filler {i}" for i in range(1, 40)])
album("A7", "Strangeways, Here We Come", "The Smiths", [f"Trk {i}" for i in range(1, 11)])
album("BIG", "The Smiths: Complete", "The Smiths", [f"Trk {i}" for i in range(1, 99)])

cases = []

cases.append(("exact album import", pl("p1", "Kurt Vile – b'lieve i'm goin down...",
    [tr(i, f"Song {i}", "A1", "b'lieve i'm goin down...", "Kurt Vile") for i in range(1, 13)]), "album"))

cases.append(("album, custom name", pl("p2", "late night driving",
    [tr(i, f"Song {i}", "A2", "Kid A", "Radiohead") for i in range(1, 11)]), "album"))

cases.append(("album missing 2 tracks", pl("p3", "Radiohead - Kid A",
    [tr(i, f"Song {i}", "A2", "Kid A", "Radiohead") for i in range(1, 9)]), "likely_album"))

cases.append(("curated mixtape", pl("p4", "Summer 2019",
    [tr(i, f"Unique {i}", f"X{i}", f"Album {i}", f"Artist {i}") for i in range(1, 21)]), "curated"))

cases.append(("artist-leaning curated", pl("p5", "Radiohead favourites",
    [tr(i, f"Song {i}", "A2", "Kid A", "Radiohead") for i in range(1, 6)]
    + [tr(i, f"Other {i}", f"Y{i}", f"Album {i}", "Radiohead") for i in range(6, 13)]), "curated"))

cases.append(("album plus extras", pl("p6", "Blonde +",
    [tr(i, f"Song {i}", "A3", "Blonde", "Frank Ocean") for i in range(1, 18)]
    + [tr(i, f"Extra {i}", f"Z{i}", f"Other {i}", "Someone") for i in range(18, 21)]), "album_plus_extras"))

cases.append(("partial album", pl("p7", "Prince picks",
    [tr(i, f"Song {i}", "A4", "Sign o' the Times", "Prince") for i in range(1, 6)]), "album_subset"))

cases.append(("album across editions", pl("p8", "The Beatles - Abbey Road",
    [tr(i, f"Song {i}", "A5", "Abbey Road", "The Beatles") for i in range(1, 10)]
    + [tr(i, f"Song {i}", "A5b", "Abbey Road (Remastered)", "The Beatles") for i in range(10, 18)]), "album"))

cases.append(("tiny playlist", pl("p9", "two songs",
    [tr(1, "Song 1", "A2", "Kid A", "Radiohead"), tr(2, "Nothing Alike", "Q1", "Something", "Else")]), "curated"))

cases.append(("empty playlist", pl("p10", "Untitled", []), "empty"))

cases.append(("named like album, curated content", pl("p11", "Radiohead - Kid A",
    [tr(i, f"Unrelated {i}", f"W{i}", f"Album {i}", f"Artist {i}") for i in range(1, 16)]), "curated"))

# Found in the live library: a Spotify import where a few tracks got matched to
# compilation releases instead of the album, so album-id grouping sees only 10/12.
cases.append(("import artifact: 2 tracks cite a compilation", pl("p12", "Tame Impala - Lonerism",
    [tr(i, f"Song {i}", "A6", "Lonerism", "Tame Impala") for i in range(1, 11)]
    + [tr(11, "Song 11", "COMP", "Workout 2019", "Tame Impala"),
       tr(12, "Song 12", "COMP", "Summer Playlist", "Tame Impala")]), "album"))

# Also live: every track cites a huge compilation, but the real album is a candidate.
cases.append(("compilation trap: prefer the album it equals", pl("p13", "The Smiths - Strangeways",
    [tr(i, f"Trk {i}", "BIG", "The Smiths: Complete", "The Smiths") for i in range(1, 11)]
    + [tr(99, "Trk 1", "A7", "Strangeways, Here We Come", "The Smiths")]), "album"))

ok = True
for label, playlist, expected in cases:
    got = classify(playlist, ALBUMS, TRACKS)
    mark = "PASS" if got["verdict"] == expected else "FAIL"
    if mark == "FAIL":
        ok = False
    print(f"{mark}  {label:<46} expected={expected:<18} got={got['verdict']:<18} "
          f"prec={got['precision']:.2f} cov={got['coverage']:.2f} name={got['name_score']:.2f}")

# A delisted album: referenced id has no metadata and no track list at all.
dead = classify(pl("p14", "Nadja – Numbness",
    [tr(i, f"Song {i}", "DEAD", "Numbness", "Nadja") for i in range(1, 7)]),
    {"DEAD": {"id": "DEAD", "error": "Object not found"}}, {})
ok &= dead["verdict"] == "unresolved"
print(f"{'PASS' if dead['verdict'] == 'unresolved' else 'FAIL'}  "
      f"{'delisted album -> unresolved, not curated':<46} "
      f"expected=unresolved         got={dead['verdict']}")

tiers = {e["name"]: e["tier"] for e in annotate([
    classify(pl("t1", "Kurt Vile – b'lieve i'm goin down...",
        [tr(i, f"Song {i}", "A1", "b'lieve i'm goin down...", "Kurt Vile") for i in range(1, 13)]), ALBUMS, TRACKS),
    classify(pl("t2", "late night driving",
        [tr(i, f"Song {i}", "A2", "Kid A", "Radiohead") for i in range(1, 11)]), ALBUMS, TRACKS),
    dead,
])}
for name, expected in [("Kurt Vile – b'lieve i'm goin down...", "A"),
                       ("late night driving", "B"),
                       ("Nadja – Numbness", "E")]:
    good = tiers.get(name) == expected
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  tier of {name[:34]:<34} expected={expected} got={tiers.get(name)}")

print("\n-- normalization --")
for a, b in [("b'lieve i'm goin down...", "B'lieve I'm Goin Down"),
             ("Abbey Road (Deluxe Edition)", "Abbey Road"),
             ("In Rainbows - Remastered 2017", "In Rainbows"),
             ("Café Bleu", "Cafe Bleu")]:
    same = normalize(a) == normalize(b)
    ok &= same
    print(f"  {'PASS' if same else 'FAIL'}  {normalize(a)!r} == {normalize(b)!r}")

print("\n" + ("CLASSIFIER TESTS PASSED" if ok else "CLASSIFIER TESTS FAILED"))

# --- artist guard: a cover album has an identical track list ------------------
album("COVER", "The Smiths Project Box Set", "Janice Whaley", [f"Mm {i}" for i in range(1, 11)])
album("REAL", "Meat Is Murder", "The Smiths", [f"Mm {i}" for i in range(1, 11)])
from tidal_cleanup.classify import artist_agrees  # noqa: E402

smiths_tracks = [tr(i, f"Mm {i}", "REAL", "Meat Is Murder", "The Smiths") for i in range(1, 11)]
guard_cases = [
    ("cover album rejected", artist_agrees(smiths_tracks, "Meat Is Murder", "Janice Whaley"), False),
    ("real album accepted", artist_agrees(smiths_tracks, "Meat Is Murder", "The Smiths"), True),
    ("'The' prefix tolerated", artist_agrees(
        [tr(1, "x", "a", "b", "The Jesus and Mary Chain")] * 5,
        "Jesus And Mary Chain - Psychocandy", "The Jesus and Mary Chain"), True),
    ("compilation matched via playlist name", artist_agrees(
        [tr(i, f"s{i}", f"a{i}", f"b{i}", f"Artist {i}") for i in range(10)],
        "Various Artists – Sound Of Copenhagen", "Sound Of Copenhagen"), True),
    ("unrelated artist rejected", artist_agrees(smiths_tracks, "Meat Is Murder", "Metallica"), False),
]
print()
guard_ok = True
for label, got, expected in guard_cases:
    good = got == expected
    guard_ok &= good
    print(f"{'PASS' if good else 'FAIL'}  {label:<46} got={got} expected={expected}")
print("\n" + ("ARTIST GUARD TESTS PASSED" if guard_ok else "ARTIST GUARD TESTS FAILED"))


# --- Dolby Atmos: an Atmos edition and a stereo edition share every title -----
# Reset the shared fixtures for a focused check.
ALBUMS.clear(); TRACKS.clear()
TITLES = [f"Cut {i}" for i in range(1, 11)]
ALBUMS["ST"] = {"id": "ST", "name": "Nightfall", "artist": "Band", "artist_id": "b1",
                "num_tracks": 10, "audio_modes": ["STEREO"], "media_tags": ["LOSSLESS"]}
ALBUMS["AT"] = {"id": "AT", "name": "Nightfall", "artist": "Band", "artist_id": "b1",
                "num_tracks": 10, "audio_modes": ["DOLBY_ATMOS"],
                "media_tags": ["DOLBY_ATMOS"]}
TRACKS["ST"] = list(TITLES)
TRACKS["AT"] = list(TITLES)


def atmos_track(i, album_id, modes):
    row = tr(i, f"Cut {i}", album_id, "Nightfall", "Band")
    row["audio_modes"] = modes
    row["media_tags"] = ["DOLBY_ATMOS"] if "DOLBY_ATMOS" in modes else ["LOSSLESS"]
    return row


# Candidate order deliberately offers the *wrong* edition first in each case.
stereo_pl = pl("ps", "Band - Nightfall",
               [atmos_track(i, "AT", ["STEREO"]) for i in range(1, 6)]
               + [atmos_track(i, "ST", ["STEREO"]) for i in range(6, 11)])
atmos_pl = pl("pa", "Band - Nightfall",
              [atmos_track(i, "ST", ["DOLBY_ATMOS"]) for i in range(1, 6)]
              + [atmos_track(i, "AT", ["DOLBY_ATMOS"]) for i in range(6, 11)])

atmos_ok = True
for label, playlist, expected_album in [
    ("stereo playlist keeps the stereo edition", stereo_pl, "ST"),
    ("atmos playlist keeps the atmos edition", atmos_pl, "AT"),
]:
    got = classify(playlist, ALBUMS, TRACKS)
    good = got["album"]["id"] == expected_album and got["verdict"] == "album"
    atmos_ok &= good
    print(f"{'PASS' if good else 'FAIL'}  {label:<46} matched={got['album']['id']} "
          f"expected={expected_album} atmos_tracks={got['atmos_tracks']}")

flagged = classify(stereo_pl, ALBUMS, TRACKS)
has_warning = not any("Atmos-only" in r for r in flagged["reasons"])
atmos_ok &= has_warning
print(f"{'PASS' if has_warning else 'FAIL'}  "
      f"{'no false atmos warning on a correct match':<46}")

mismatch = classify(atmos_pl, {"ST": ALBUMS["ST"]}, {"ST": TRACKS["ST"]})
warned = any("stereo-only" in r for r in mismatch["reasons"])
atmos_ok &= warned
print(f"{'PASS' if warned else 'FAIL'}  "
      f"{'warns when atmos playlist gets a stereo album':<46}")

print("\n" + ("ATMOS TESTS PASSED" if atmos_ok else "ATMOS TESTS FAILED"))

everything = ok and guard_ok and atmos_ok
print("\n" + ("ALL TESTS PASSED" if everything else "SOME TESTS FAILED"))
sys.exit(0 if everything else 1)
