"""Upgrade saved stereo albums to their Dolby Atmos editions.

Tidal publishes an Atmos release as a *separate album* with its own id and the
same title, so finding one means listing everything by that artist — search does
not reliably surface it, and `artists/{id}/albums` needs `filter=ALL` (which
returned 243 albums where the default returned 193 for one artist tested).

Nothing is swapped unless the Atmos edition provably contains every track of the
stereo one, so an upgrade cannot cost coverage.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Set

import tidalapi

from . import fetch
from .classify import ATMOS, normalize, title_set
from .paths import ATMOS_CACHE, ensure_dirs

ARTIST_PAGE = 50
# An artist with a catalogue this large is a "Various Artists" style bucket; paging
# it costs far more than it can plausibly return.
MAX_ARTIST_ALBUMS = 600
TITLE_MATCH = 0.85
# A retitled Atmos edition is accepted only on much stronger tracklist evidence
# than a same-titled one, since the title is no longer corroborating anything.
LOOSE_RECALL = 0.85
# Plausible size ratio before spending a request on a candidate's track list.
SIZE_WINDOW = (0.6, 1.8)

# Within a single artist, a live or re-recorded record can carry exactly the same
# track titles as the studio album. Two classes matter, and they are treated
# differently:
#
# HARD — a different performance altogether. Never the album you own, so such a
# candidate is dropped rather than offered.
HARD_MARKERS = (
    "live", "in concert", "unplugged", "acoustic", "demo", "demos", "instrumental",
    "karaoke", "tribute", "re-recorded", "rerecorded", "taylor's version",
    "radio session", "sessions", "bbc", "a cappella", "rehearsal", "bootleg",
    "commentary", "soundtrack", "score",
)
# SOFT — plausibly the same recording reworked (a new mix, a reissue under another
# name). These are surfaced for review rather than trusted or discarded, because
# the title alone cannot settle it.
SOFT_MARKERS = (
    "redux", "reimagined", "revisited", "remix", "remixes", "dub", "mono",
)
VERSION_MARKERS = HARD_MARKERS + SOFT_MARKERS


# The shortest normalized title we will accept as a prefix. Below this, a short
# title could prefix-match an unrelated longer one.
MIN_PREFIX = 4


def _prefix_pair(a: str, b: str) -> bool:
    """True when one title is the other plus a trailing suffix, at a word boundary.

    Atmos releases routinely append the mix name to every track — "Black Peter"
    becomes "Black Peter (2023 Mickey Hart Mix)" — so requiring exact equality
    rejects the whole album. Matching on a word-boundary prefix handles that
    without having to enumerate every label's naming habit.
    """
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < MIN_PREFIX or short == long:
        return short == long
    return long.startswith(short + " ")


def match_tracklists(mine: Set[str], theirs: Set[str]) -> Dict[str, Any]:
    """Pair two track lists, tolerating edition suffixes on either side.

    Each candidate title is consumed at most once, so a compilation cannot match
    the same track repeatedly.
    """
    remaining = set(theirs)
    matched: Set[str] = set()
    consumed: Set[str] = set()

    for title in mine:                      # exact matches first
        if title in remaining:
            matched.add(title)
            remaining.discard(title)
            consumed.add(title)

    for title in sorted(mine - matched):    # then suffix-tolerant
        for candidate in sorted(remaining):
            if _prefix_pair(title, candidate):
                matched.add(title)
                remaining.discard(candidate)
                consumed.add(candidate)
                break

    precision = len(matched) / len(mine) if mine else 0.0
    recall = len(consumed) / len(theirs) if theirs else 0.0
    return {
        "matched": matched,
        "missing": sorted(mine - matched),
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "extra": len(theirs) - len(consumed),
    }


def _version_markers(text: Optional[str]) -> Set[str]:
    low = (text or "").lower()
    return {m for m in VERSION_MARKERS if m in low}


def _same_kind_of_record(mine: Optional[str], theirs: Optional[str]) -> bool:
    """True when the candidate claims to be the same kind of recording."""
    return not (_version_markers(theirs) - _version_markers(mine))


def _different_performance(mine: Optional[str], theirs: Optional[str]) -> bool:
    """True when the candidate is a different performance, not a remix or reissue."""
    hard = {m for m in HARD_MARKERS}
    return bool((_version_markers(theirs) & hard) - _version_markers(mine))


def _load() -> Dict[str, Any]:
    if ATMOS_CACHE.exists():
        try:
            data = json.loads(ATMOS_CACHE.read_text())
            data.setdefault("artist_albums", {})
            data.setdefault("album_tracks", {})
            data.setdefault("albums", {})
            return data
        except Exception:
            pass
    return {"artist_albums": {}, "album_tracks": {}, "albums": {}}


def _save(cache: Dict[str, Any]) -> None:
    ensure_dirs()
    tmp = ATMOS_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False))
    tmp.replace(ATMOS_CACHE)


def _is_atmos(modes: Optional[List[str]]) -> bool:
    return ATMOS in (modes or [])


def artist_albums(
    session: tidalapi.Session, artist_id: str, cache: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Every album Tidal lists for an artist, cached. Uses filter=ALL."""
    key = str(artist_id)
    if key not in cache["artist_albums"]:
        cache["artist_albums"][key] = _fetch_artist_albums(session, key)
    return cache["artist_albums"][key]


def _fetch_artist_albums(
    session: tidalapi.Session, artist_id: str
) -> List[Dict[str, Any]]:
    """One artist's whole catalogue. filter=ALL is required: without it Tidal
    omits a large share of releases (193 albums / 12 Atmos vs 243 / 19 for one
    artist tested)."""
    key = str(artist_id)
    rows: Dict[str, Dict[str, Any]] = {}
    offset = 0
    while True:
        try:
            payload = fetch.get_json(
                session,
                f"artists/{key}/albums",
                params={"limit": ARTIST_PAGE, "offset": offset, "filter": "ALL"},
            )
        except Exception:
            break
        items = payload.get("items") or []
        for item in items:
            if item.get("id") is None:
                continue
            rows[str(item["id"])] = {
                "id": str(item["id"]),
                "title": item.get("title"),
                "num_tracks": item.get("numberOfTracks"),
                "audio_modes": item.get("audioModes") or [],
                "media_tags": (item.get("mediaMetadata") or {}).get("tags") or [],
                "release_date": item.get("releaseDate"),
            }
        offset += ARTIST_PAGE
        total = payload.get("totalNumberOfItems") or 0
        if len(items) < ARTIST_PAGE or offset >= total or offset >= MAX_ARTIST_ALBUMS:
            break
    return list(rows.values())


def _titles(
    session: tidalapi.Session, album_id: str, cache: Dict[str, Any]
) -> List[str]:
    key = str(album_id)
    if key not in cache["album_tracks"]:
        try:
            cache["album_tracks"][key] = fetch.album_track_titles(session, key)
        except Exception:
            cache["album_tracks"][key] = []
    return cache["album_tracks"][key]


def saved_albums(
    session: tidalapi.Session,
    album_ids: Set[str],
    cache: Optional[Dict[str, Any]] = None,
    workers: int = fetch.DEFAULT_WORKERS,
    verbose: bool = True,
) -> List[Dict[str, Any]]:
    """Full records for every album in the collection, cached across runs.

    Album metadata does not change, so re-reading 750 of them on every run is pure
    cost; only ids not seen before are fetched.
    """
    cache = cache if cache is not None else {"albums": {}}
    cache.setdefault("albums", {})
    ids = sorted(str(a) for a in album_ids)
    missing = [a for a in ids if a not in cache["albums"]]
    if verbose:
        print(
            f"{len(ids)} saved album(s); {len(ids) - len(missing)} cached, "
            f"{len(missing)} to read.",
            file=sys.stderr,
        )
    if missing:
        results = fetch.parallel_map(
            session,
            missing,
            lambda s, a: fetch.album_row(s, a),
            label="albums",
            workers=workers,
            verbose=verbose,
        )
        for index, album_id in enumerate(missing):
            value = results.get(index)
            if isinstance(value, dict) and not value.get("__error__"):
                cache["albums"][album_id] = value
    return [cache["albums"][a] for a in ids if a in cache["albums"]]


# Tokens that begin an edition/mix suffix rather than part of the album's name.
# A normalized title is truncated at the first of these, so "American Beauty
# (Atmos Mix)" and "American Beauty (2013 Remaster)" share a base title.
_SUFFIX_TOKENS = {
    "atmos", "dolby", "mix", "mixes", "remix", "remixes", "remaster", "remastered",
    "remasters", "mono", "stereo", "anniversary", "deluxe", "expanded", "edition",
    "version", "reissue", "redux", "reimagined", "revisited", "spatial",
}


def _is_year(token: str) -> bool:
    return len(token) == 4 and token.isdigit() and 1900 <= int(token) <= 2099


def base_title(text: Optional[str]) -> str:
    """The album's name with any edition/mix suffix removed.

    "Workingman's Dead (2023 Mickey Hart Mix)" -> "workingman s dead", so it can be
    recognised as the same record as a plain "Workingman's Dead". The first token is
    never dropped, so an album actually called "Remaster" survives.
    """
    tokens = normalize(text).split()
    for index, token in enumerate(tokens):
        if index == 0:
            continue
        if token in _SUFFIX_TOKENS or _is_year(token):
            return " ".join(tokens[:index])
    return " ".join(tokens)


def _title_match(a: str, b: str) -> bool:
    import difflib

    if not a or not b:
        return False
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= TITLE_MATCH


def find_upgrades(
    session: tidalapi.Session,
    album_ids: Set[str],
    workers: int = fetch.DEFAULT_WORKERS,
    verbose: bool = True,
    loose: bool = True,
) -> Dict[str, Any]:
    """Find an Atmos edition for each saved stereo album, verified track by track.

    Runs as four fetch phases, each parallel and each cached, rather than walking
    artists one at a time: reading ~500 artist catalogues serially is what made an
    earlier version of this take over ten minutes and look like a hang.
    """
    cache = _load()

    # Phase 1: the albums in the collection, straight from the favourites listing
    # (which already embeds artist and audioModes) rather than one fetch each.
    albums = fetch.favorite_albums(session, verbose=verbose)
    if album_ids:
        wanted = {str(a) for a in album_ids}
        albums = [a for a in albums if a["id"] in wanted]

    already = [a for a in albums if _is_atmos(a.get("audio_modes"))]
    stereo = [
        a for a in albums
        if not _is_atmos(a.get("audio_modes")) and a.get("artist_id")
    ]
    no_artist = [a for a in albums if not a.get("artist_id")]
    if verbose:
        print(
            f"{len(already)} already Atmos · {len(stereo)} stereo to check · "
            f"{len(no_artist)} without an artist id",
            file=sys.stderr,
        )

    by_artist: Dict[str, List[Dict[str, Any]]] = {}
    for album in stereo:
        by_artist.setdefault(str(album["artist_id"]), []).append(album)

    # Phase 2: every relevant artist's catalogue, in parallel.
    pending = sorted(a for a in by_artist if a not in cache["artist_albums"])
    if verbose:
        print(
            f"{len(by_artist)} artist(s); {len(by_artist) - len(pending)} cached, "
            f"{len(pending)} to read.",
            file=sys.stderr,
        )
    if pending:
        results = fetch.parallel_map(
            session,
            pending,
            lambda s, artist_id: _fetch_artist_albums(s, artist_id),
            label="artist catalogues",
            workers=workers,
            verbose=verbose,
        )
        for index, artist_id in enumerate(pending):
            value = results.get(index)
            cache["artist_albums"][artist_id] = (
                [] if isinstance(value, dict) and value.get("__error__") else value
            )
        _save(cache)

    # Phase 3: pair saved albums with Atmos editions. Two routes: an edition with
    # effectively the same title, or one whose tracklist matches even though it has
    # been retitled. No requests here.
    pairs: List[Any] = []
    for artist_id, mine in by_artist.items():
        editions = [
            e
            for e in cache["artist_albums"].get(artist_id) or []
            if _is_atmos(e.get("audio_modes"))
        ]
        if not editions:
            continue
        for album in mine:
            want = base_title(album.get("name"))
            for edition in editions:
                if edition["id"] == str(album["id"]):
                    continue
                if _title_match(want, base_title(edition.get("title"))):
                    # Same base title, but if the candidate advertises a different
                    # kind of recording (a remix, a live take) the title alone is
                    # no longer trustworthy — send it for review instead.
                    kind = (
                        "title"
                        if _same_kind_of_record(
                            album.get("name"), edition.get("title")
                        )
                        else "variant"
                    )
                    pairs.append((album, edition, kind))
                    continue
                if not loose:
                    continue
                # Retitled candidate: only worth a track-list request if it is a
                # plausible size and not a live/re-recorded version of the album.
                size, mine_size = edition.get("num_tracks"), album.get("num_tracks")
                if size and mine_size:
                    ratio = mine_size / size
                    if not SIZE_WINDOW[0] <= ratio <= SIZE_WINDOW[1]:
                        continue
                # A different performance is never the album you own. A mere
                # reworking is offered for review instead of being discarded.
                if _different_performance(album.get("name"), edition.get("title")):
                    continue
                pairs.append((album, edition, "tracklist"))

    by_kind = Counter(kind for _, _, kind in pairs)
    if verbose:
        print(
            f"{len(pairs)} candidate pair(s) to verify track by track "
            f"({by_kind['title']} by title, {by_kind['variant']} same title but a "
            f"different kind of release, {by_kind['tracklist']} retitled).",
            file=sys.stderr,
        )

    # Phase 4: the track lists both sides of each pair need, in parallel.
    involved = {str(a["id"]) for a, _, _ in pairs} | {str(e["id"]) for _, e, _ in pairs}
    needed = sorted(involved - set(cache["album_tracks"]))
    if needed:
        if verbose:
            print(f"{len(needed)} album track list(s) to read.", file=sys.stderr)
        results = fetch.parallel_map(
            session,
            needed,
            lambda s, a: fetch.album_track_titles(s, a),
            label="track lists",
            workers=workers,
            verbose=verbose,
        )
        for index, album_id in enumerate(needed):
            value = results.get(index)
            cache["album_tracks"][album_id] = (
                [] if isinstance(value, dict) and value.get("__error__") else value
            )
        _save(cache)

    # Phase 5: verify locally. An Atmos edition missing any track is rejected; a
    # retitled one must additionally cover nearly all of itself, so a greatest-hits
    # record that happens to contain the album cannot pass.
    upgrades: Dict[str, Dict[str, Any]] = {}
    retitled: Dict[str, Dict[str, Any]] = {}
    partial: List[Dict[str, Any]] = []
    for album, edition, kind in pairs:
        mine_titles = title_set(cache["album_tracks"].get(str(album["id"])) or [])
        their_titles = title_set(cache["album_tracks"].get(str(edition["id"])) or [])
        if not mine_titles or not their_titles:
            continue
        result = match_tracklists(mine_titles, their_titles)
        record = {
            "stereo": album,
            "atmos": edition,
            "missing": result["missing"],
            "match": kind,
            "recall": result["recall"],
            "precision": result["precision"],
            "stereo_tracks": len(mine_titles),
            "atmos_tracks": len(their_titles),
            "extra": result["extra"],
        }
        if result["missing"]:
            # A candidate for a *different* album by the same artist is not a near
            # miss, it is just a comparison that failed; only same-titled pairings
            # are worth a human's attention.
            record["plausible"] = base_title(album.get("name")) == base_title(
                edition.get("title")
            )
            partial.append(record)
            continue
        if kind != "title" and result["recall"] < LOOSE_RECALL:
            record["rejected"] = (
                f"only covers {result['recall']:.0%} of the Atmos edition"
            )
            record["plausible"] = base_title(album.get("name")) == base_title(
                edition.get("title")
            )
            partial.append(record)
            continue

        # Only an exact-kind title match is trusted without review.
        bucket = upgrades if kind == "title" else retitled
        key = str(album["id"])
        best = bucket.get(key)
        # Prefer the Atmos edition closest in size to what is saved.
        if best is None or abs(record["atmos_tracks"] - record["stereo_tracks"]) < abs(
            best["atmos_tracks"] - best["stereo_tracks"]
        ):
            bucket[key] = record

    # A title match always wins over a retitled one for the same album.
    retitled = {k: v for k, v in retitled.items() if k not in upgrades}

    # Keep only the best (least lossy) near miss per saved album, and never offer
    # one for an album that already has a complete Atmos edition — a lossy option
    # is strictly worse than the lossless one sitting next to it.
    settled = {str(r["stereo"]["id"]) for r in list(upgrades.values()) + list(retitled.values())}
    best_near: Dict[str, Dict[str, Any]] = {}
    for record in partial:
        if not record.get("plausible") or record.get("rejected"):
            continue
        if str(record["stereo"]["id"]) in settled:
            continue
        key = str(record["stereo"]["id"])
        current = best_near.get(key)
        if current is None or len(record["missing"]) < len(current["missing"]):
            best_near[key] = record
    near_misses = sorted(best_near.values(), key=lambda r: len(r["missing"]))

    _save(cache)
    return {
        "upgrades": list(upgrades.values()),
        "retitled": list(retitled.values()),
        "near_misses": near_misses,
        "discarded": len([r for r in partial if not r.get("plausible")]),
        "partial": partial,
        "already_atmos": already,
        "stereo_checked": len({str(a["id"]) for a, _, _ in pairs}),
        "stereo_total": len(stereo),
        "no_artist": no_artist,
    }


def apply_upgrades(
    session: tidalapi.Session,
    collection,
    upgrades: List[Dict[str, Any]],
    remove_stereo: bool = True,
    dry_run: bool = False,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Save the Atmos editions, then drop the stereo ones they replace.

    The stereo album is only removed after the Atmos edition is confirmed present
    in the collection, so a failed save never leaves you with neither.
    """
    from . import actions

    summary: Dict[str, Any] = {
        "requested": len(upgrades),
        "atmos_saved": 0,
        "stereo_removed": 0,
        "unconfirmed": [],
        "remove_failed": [],
        "dry_run": dry_run,
    }
    if not upgrades:
        return summary

    atmos_ids = [u["atmos"]["id"] for u in upgrades]
    if dry_run:
        summary["atmos_saved"] = len(
            [a for a in atmos_ids if not collection.has_album(a)]
        )
        summary["stereo_removed"] = len(upgrades) if remove_stereo else 0
        return summary

    before = set(collection.album_ids)
    if verbose:
        print(f"Saving {len(atmos_ids)} Atmos album(s)...", file=sys.stderr)
    collection.add_albums(atmos_ids)
    confirmed = collection.refresh_albums()
    summary["atmos_saved"] = len(confirmed - before)
    summary["unconfirmed"] = [a for a in atmos_ids if a not in confirmed]

    for upgrade in upgrades:
        atmos_id = upgrade["atmos"]["id"]
        stereo_id = str(upgrade["stereo"]["id"])
        ok = atmos_id in confirmed
        removed = False
        if ok and remove_stereo and stereo_id in confirmed:
            # remove_album reports failure by returning False as well as by raising.
            try:
                removed = bool(session.user.favorites.remove_album(stereo_id))
                error = None if removed else "request reported failure"
            except Exception as exc:
                error = str(exc)
            if removed:
                summary["stereo_removed"] += 1
            else:
                summary["remove_failed"].append(
                    {"album_id": stereo_id, "name": upgrade["stereo"].get("name"),
                     "error": error}
                )
        actions.log_action(
            {
                "action": "atmos_upgrade",
                "stereo_album_id": stereo_id,
                "stereo_album_name": upgrade["stereo"].get("name"),
                "artist": upgrade["stereo"].get("artist"),
                "atmos_album_id": atmos_id,
                "atmos_album_name": upgrade["atmos"].get("title"),
                "atmos_saved": ok,
                "stereo_removed": removed,
                "stereo_tracks": upgrade["stereo_tracks"],
                "atmos_tracks": upgrade["atmos_tracks"],
                "match": upgrade.get("match"),
                "tracks_lost": upgrade.get("missing") or [],
            }
        )
    collection.refresh_albums()
    return summary


def cached_tracklist(album_id: str) -> List[str]:
    """Track titles already read for an album, for display during review."""
    return (_load().get("album_tracks") or {}).get(str(album_id)) or []


def review_candidates(found: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Everything that wants a human decision: retitled matches, then near misses.

    Lossless-but-retitled first, then near misses ordered by how little they cost,
    so the easiest calls come before the ones worth refusing.
    """
    retitled = sorted(
        found.get("retitled") or [], key=lambda r: str(r["stereo"].get("name") or "")
    )
    near = sorted(found.get("near_misses") or [], key=lambda r: len(r["missing"]))
    return retitled + near
