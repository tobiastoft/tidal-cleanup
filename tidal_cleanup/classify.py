"""Decide whether a playlist is really just an album.

The decisive evidence is the track list, not the name: an album-import has (a) nearly
all its tracks pointing at one album and (b) nearly all of that album's tracks present.
The name is only a confidence booster, so a playlist renamed by hand is still detected
and a hand-curated playlist that happens to be named after an album is still protected.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# A playlist must be at least this album-like before we even fetch its album metadata.
MIN_CONCENTRATION_TO_CHECK = 0.5

DASHES = "‐‑‒–—―−"
_EDITION_WORDS = (
    r"deluxe|expanded|remaster(?:ed)?|anniversary|edition|version|reissue|bonus|"
    r"explicit|clean|mono|stereo|digital|special|collector'?s|super|platinum|gold|"
    r"legacy|complete|definitive|extended|re-?recorded|taylor'?s"
)
_BRACKETED_EDITION = re.compile(
    rf"[\(\[][^)\]]*\b(?:{_EDITION_WORDS})\b[^)\]]*[\)\]]", re.IGNORECASE
)
_TRAILING_EDITION = re.compile(
    rf"\s-\s[^-]*\b(?:{_EDITION_WORDS}|ep|single)\b[^-]*$", re.IGNORECASE
)

VERDICT_CONVERT = ("album", "likely_album")
VERDICT_REVIEW = ("album_plus_extras", "album_subset")


def normalize(text: Optional[str]) -> str:
    """Fold case, accents, edition suffixes and punctuation into a comparable key."""
    if not text:
        return ""
    value = unicodedata.normalize("NFKD", text)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    for dash in DASHES:
        value = value.replace(dash, "-")
    value = _BRACKETED_EDITION.sub(" ", value)
    value = _TRAILING_EDITION.sub(" ", value)
    value = re.sub(r"[^0-9a-zA-Z]+", " ", value)
    return " ".join(value.lower().split())


def split_on_dash(name: str) -> List[str]:
    """Return the sides of an "Artist - Album" style title, if it looks like one."""
    pattern = re.compile(rf"\s[-{DASHES}]\s")
    parts = [p.strip() for p in pattern.split(name) if p.strip()]
    return parts if len(parts) >= 2 else []


def name_similarity(playlist_name: str, album_name: str, album_artist: str) -> float:
    """Best match between how the playlist is titled and how the album is titled."""
    album_variants = [
        normalize(album_name),
        normalize(f"{album_artist} {album_name}"),
        normalize(f"{album_name} {album_artist}"),
    ]
    playlist_variants = [normalize(playlist_name)]
    playlist_variants += [normalize(p) for p in split_on_dash(playlist_name)]

    best = 0.0
    for left in playlist_variants:
        for right in album_variants:
            if not left or not right:
                continue
            best = max(best, difflib.SequenceMatcher(None, left, right).ratio())
    return round(best, 3)


ATMOS = "DOLBY_ATMOS"


def is_atmos(record: Optional[Dict[str, Any]]) -> bool:
    """True when a track or album record advertises Dolby Atmos."""
    if not record:
        return False
    return ATMOS in (record.get("audio_modes") or []) or ATMOS in (
        record.get("media_tags") or []
    )


def atmos_only(record: Optional[Dict[str, Any]]) -> bool:
    """Atmos with no stereo alternative — a distinct release, not just a flag."""
    modes = (record or {}).get("audio_modes") or []
    return modes == [ATMOS]


def artist_agrees(
    tracks: List[Dict[str, Any]], playlist_name: str, album_artist: Optional[str]
) -> bool:
    """Does an album candidate come from the same artist as the playlist?

    Title matching alone is not enough: a full cover album (say Janice Whaley's
    a cappella version of "Meat Is Murder") has an identical track list and scores
    a perfect match, so adopting it would save the wrong record and delete the
    playlist. The performer has to agree too.
    """
    if not album_artist:
        return False
    target = normalize(album_artist)
    if not target:
        return False

    counts = Counter(
        normalize(t.get("album_artist") or t.get("artist"))
        for t in tracks
        if (t.get("album_artist") or t.get("artist"))
    )
    for candidate, _ in counts.most_common(3):
        if not candidate:
            continue
        if candidate == target or candidate in target or target in candidate:
            return True
        if difflib.SequenceMatcher(None, candidate, target).ratio() >= 0.70:
            return True

    # A compilation's tracks have many different performers; fall back to the
    # playlist's own title, which usually names the artist.
    name = normalize(playlist_name)
    if name and (
        target in name or difflib.SequenceMatcher(None, name, target).ratio() >= 0.70
    ):
        return True
    return False


def candidate_album_ids(tracks: List[Dict[str, Any]], limit: int = 4) -> List[str]:
    """The album ids worth testing a playlist against, most-referenced first."""
    counts = Counter(
        str(t["album_id"]) for t in tracks if t.get("album_id")
    )
    return [aid for aid, _ in counts.most_common(limit)]


def all_candidate_album_ids(
    playlists: List[Dict[str, Any]], limit: int = 4
) -> Set[str]:
    """Every album id any playlist might be matched against, for the fetch pass."""
    wanted: Set[str] = set()
    for playlist in playlists:
        tracks = playlist.get("tracks") or []
        if len(tracks) < 2:
            continue
        wanted.update(candidate_album_ids(tracks, limit=limit))
    return wanted


# Kept so an older snapshot without album track lists still classifies.
def dominant_album_ids(playlists: List[Dict[str, Any]]) -> Set[str]:
    return all_candidate_album_ids(playlists, limit=1)


def title_set(names: Iterable[Optional[str]]) -> Set[str]:
    return {normalize(n) for n in names if n and normalize(n)}


def score_against(playlist_titles: Set[str], album_titles: Set[str]) -> Dict[str, float]:
    """How well a playlist and an album match, by track title.

    precision: share of the playlist that is on the album (1.0 = nothing would be
    lost by deleting the playlist). recall: share of the album the playlist holds.
    """
    if not playlist_titles or not album_titles:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "shared": 0}
    shared = playlist_titles & album_titles
    precision = len(shared) / len(playlist_titles)
    recall = len(shared) / len(album_titles)
    f1 = (
        2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    )
    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "shared": len(shared),
    }


def _best_match(
    playlist_titles: Set[str],
    candidates: List[str],
    album_tracks: Dict[str, List[str]],
    albums: Optional[Dict[str, Any]] = None,
    playlist_is_atmos: bool = False,
) -> Tuple[Optional[str], Dict[str, float]]:
    """Pick the album the playlist most nearly *equals*.

    Scoring by F1 rather than by how many tracks cite an album id is what keeps a
    10-track album from being matched to the 98-track compilation its tracks
    happen to be filed under.

    A Dolby Atmos edition and its stereo counterpart have identical track titles,
    so they score identically; the tie is broken towards the format the playlist's
    own tracks are in, rather than by whichever happened to be checked first.
    """
    albums = albums or {}
    best_id: Optional[str] = None
    best: Dict[str, float] = {"precision": 0.0, "recall": 0.0, "f1": 0.0, "shared": 0}
    for album_id in candidates:
        album_id = str(album_id)
        titles = album_tracks.get(album_id)
        if not titles:
            continue
        result = score_against(playlist_titles, title_set(titles))
        if result["f1"] > best["f1"] + 1e-9:
            best_id, best = album_id, result
        elif best_id is not None and abs(result["f1"] - best["f1"]) <= 1e-9:
            # Equal match: prefer the edition whose format matches the playlist.
            challenger = atmos_only(albums.get(album_id)) == playlist_is_atmos
            incumbent = atmos_only(albums.get(best_id)) == playlist_is_atmos
            if challenger and not incumbent:
                best_id, best = album_id, result
    return best_id, best


def classify(
    playlist: Dict[str, Any],
    albums: Dict[str, Any],
    album_tracks: Optional[Dict[str, List[str]]] = None,
    resolved: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Score one playlist and return the verdict plus the evidence behind it."""
    album_tracks = album_tracks or {}
    resolved = resolved or {}
    tracks = playlist.get("tracks") or []
    total = len(tracks)
    result: Dict[str, Any] = {
        "playlist_id": playlist["id"],
        "name": playlist["name"],
        "num_tracks": total,
        "num_videos": playlist.get("num_videos") or 0,
        "description": playlist.get("description") or "",
        "share_url": playlist.get("share_url"),
        "verdict": "curated",
        "reasons": [],
        "album": None,
        "concentration": 0.0,
        "coverage": 0.0,
        "precision": 0.0,
        "name_score": 0.0,
        "distinct_albums": 0,
        "extra_tracks": 0,
        "atmos_tracks": 0,
    }

    if total == 0:
        result["verdict"] = "empty"
        result["reasons"].append("playlist has no tracks")
        return result

    result["distinct_albums"] = len({t["album_id"] for t in tracks if t.get("album_id")})
    if total < 2:
        result["verdict"] = "too_small"
        result["reasons"].append("single track, too ambiguous to judge")
        return result

    playlist_titles = title_set(t.get("name") for t in tracks)
    if not playlist_titles:
        result["reasons"].append("no usable track titles")
        return result

    candidates = candidate_album_ids(tracks)
    if resolved.get(playlist["id"]):
        candidates = [resolved[playlist["id"]]] + [
            c for c in candidates if c != resolved[playlist["id"]]
        ]

    atmos_tracks = sum(1 for t in tracks if is_atmos(t))
    result["atmos_tracks"] = atmos_tracks
    playlist_is_atmos = atmos_tracks * 2 >= total

    album_id, best = _best_match(
        playlist_titles, candidates, album_tracks, albums, playlist_is_atmos
    )

    # Album id counts are still reported, as a hint about import quality.
    if candidates:
        top = Counter(
            str(t["album_id"]) for t in tracks if t.get("album_id")
        ).most_common(1)
        if top:
            result["concentration"] = round(top[0][1] / total, 3)

    if album_id is None:
        # No album track list to compare against. Fall back to the album's declared
        # track count, and if even that is missing say so rather than defaulting to
        # "curated" — a failed lookup is not evidence of a hand-made playlist.
        return _classify_without_track_lists(
            result, playlist, tracks, candidates, albums, total
        )

    album = albums.get(album_id) or {}
    album_titles = title_set(album_tracks.get(album_id) or [])
    off_album = sorted(playlist_titles - album_titles)
    name_score = name_similarity(
        playlist["name"], album.get("name") or "", album.get("artist") or ""
    )

    result["album"] = {
        "id": album.get("id") or album_id,
        "name": album.get("name"),
        "artist": album.get("artist"),
        "artist_id": album.get("artist_id"),
        "num_tracks": album.get("num_tracks") or len(album_titles),
        "release_date": album.get("release_date"),
        "share_url": album.get("share_url"),
        "resolved_from": album.get("resolved_from"),
        "audio_modes": album.get("audio_modes") or [],
        "atmos": is_atmos(album),
        "atmos_only": atmos_only(album),
    }
    result["precision"] = best["precision"]
    result["coverage"] = best["recall"]
    result["f1"] = best["f1"]
    result["name_score"] = name_score
    result["album_tracks_present"] = best["shared"]
    result["extra_tracks"] = len(off_album)
    result["off_album_titles"] = off_album[:12]

    precision, recall = best["precision"], best["recall"]

    # Nothing is lost by deleting a playlist whose every track is on the album.
    if precision >= 0.999:
        if recall >= 0.85:
            verdict = "album"
        elif recall >= 0.70:
            verdict = "likely_album"
        else:
            verdict = "album_subset"
    elif recall >= 0.85 and precision >= 0.60:
        verdict = "album_plus_extras"
    elif best["f1"] >= 0.70:
        verdict = "album_plus_extras"
    else:
        verdict = "curated"

    result["verdict"] = verdict
    result["reasons"].insert(
        0,
        f"{best['shared']}/{len(album_titles)} of '{album.get('name')}' present; "
        f"{len(off_album)} of {len(playlist_titles)} playlist track(s) not on it",
    )
    if off_album:
        result["reasons"].append("not on the album: " + ", ".join(off_album[:5]))
    if result["num_videos"]:
        result["reasons"].append(f"{result['num_videos']} video item(s) present")
    if atmos_tracks:
        result["reasons"].append(
            f"{atmos_tracks}/{total} track(s) are Dolby Atmos"
        )
    if playlist_is_atmos and not result["album"]["atmos"]:
        result["reasons"].append(
            "playlist is mostly Atmos but the matched album is stereo-only"
        )
    elif result["album"]["atmos_only"] and not playlist_is_atmos:
        result["reasons"].append(
            "matched album is Atmos-only but the playlist is stereo"
        )
    return result


def _classify_without_track_lists(
    result: Dict[str, Any],
    playlist: Dict[str, Any],
    tracks: List[Dict[str, Any]],
    candidates: List[str],
    albums: Dict[str, Any],
    total: int,
) -> Dict[str, Any]:
    """Older/degraded path: judge from the album's declared track count alone."""
    album_id = candidates[0] if candidates else None
    album = albums.get(str(album_id)) if album_id else None
    group = [t for t in tracks if str(t.get("album_id")) == str(album_id)]
    concentration = len(group) / total if total else 0.0
    result["concentration"] = round(concentration, 3)

    if not album or album.get("error") or not album.get("num_tracks"):
        sample = group[0] if group else tracks[0]
        result["album_guess"] = {
            "id": str(album_id) if album_id else None,
            "name": sample.get("album_name"),
            "artist": sample.get("album_artist"),
            "error": (album or {}).get("error"),
        }
        if concentration >= 0.85:
            result["verdict"] = "unresolved"
            result["reasons"].append(
                f"{len(group)}/{total} tracks are from "
                f"'{result['album_guess']['name']}', but that album could not be "
                f"looked up on Tidal"
            )
        else:
            result["reasons"].append(
                f"only {len(group)}/{total} tracks share an album"
            )
        return result

    unique = title_set(t.get("name") for t in group)
    coverage = min(len(unique) / album["num_tracks"], 1.0)
    extra = total - len(group)
    name_score = name_similarity(
        playlist["name"], album["name"], album.get("artist") or ""
    )
    result["album"] = {
        "id": album["id"],
        "name": album["name"],
        "artist": album.get("artist"),
        "artist_id": album.get("artist_id"),
        "num_tracks": album["num_tracks"],
        "release_date": album.get("release_date"),
        "share_url": album.get("share_url"),
    }
    result["coverage"] = round(coverage, 3)
    result["precision"] = round(len(group) / total, 3)
    result["name_score"] = name_score
    result["extra_tracks"] = extra
    result["album_tracks_present"] = len(unique)

    if extra == 0:
        verdict = "album" if coverage >= 0.85 else (
            "likely_album" if coverage >= 0.70 else "album_subset"
        )
    elif coverage >= 0.90 and concentration >= 0.60:
        verdict = "album_plus_extras"
    elif concentration >= 0.85 and coverage >= 0.70:
        verdict = "album_plus_extras"
    else:
        verdict = "curated"
    result["verdict"] = verdict
    result["reasons"].insert(
        0,
        f"{len(group)}/{total} tracks are from '{album['name']}' "
        f"({len(unique)}/{album['num_tracks']} of the album present)",
    )
    if extra:
        result["reasons"].append(f"{extra} track(s) are not on that album")
    return result


def classify_all(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    albums = snapshot.get("albums") or {}
    album_tracks = snapshot.get("album_tracks") or {}
    resolved = snapshot.get("resolved") or {}
    return [
        classify(p, albums, album_tracks, resolved)
        for p in snapshot.get("playlists") or []
    ]


# --------------------------------------------------------------- tiers

# How closely the playlist title must match the album title to count as a second,
# independent confirmation rather than just track evidence.
NAME_CONFIRMS = 0.80

TIER_LABELS = {
    "A": "double-confirmed (tracks and name agree)",
    "B": "track-confirmed (name doesn't match the album)",
    "C": "needs eyes (incomplete, or holds non-album tracks)",
    "D": "curated / untouchable",
    "E": "album-shaped, but the album won't resolve on Tidal — try `resolve`",
    "F": "empty or single-track",
}
TIER_ORDER = ["A", "B", "C", "D", "E", "F"]


def tier(entry: Dict[str, Any]) -> str:
    """Group a verdict by how much human attention it deserves.

    A is safe to apply in bulk: every track belongs to one album, nearly all of
    that album is present, nothing would be lost, and the playlist is *named*
    after the album too. B has the same track proof but was renamed by hand, so
    it is likelier to be deliberate and deserves a skim. C can lose tracks.
    """
    verdict = entry["verdict"]
    name_ok = entry.get("name_score", 0.0) >= NAME_CONFIRMS

    if verdict == "album":
        return "A" if name_ok else "B"
    if verdict == "likely_album":
        return "B" if name_ok else "C"
    if verdict in ("album_plus_extras", "album_subset"):
        return "C"
    if verdict == "unresolved":
        return "E"
    if verdict in ("empty", "too_small"):
        return "F"
    return "D"


def annotate(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for entry in entries:
        entry["tier"] = tier(entry)
    return entries
