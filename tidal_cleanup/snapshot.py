"""Build a local snapshot of every owned playlist, resumably.

Two caches make re-runs cheap at a thousand playlists:
  * track lists, keyed on the playlist's own lastUpdated stamp, so an unchanged
    playlist is never re-fetched;
  * album metadata, kept forever, since an album's track count doesn't change.
Both are checkpointed as results land, so an interrupted run resumes instead of
starting over.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import tidalapi

from . import fetch
from .paths import CACHE_FILE, SNAPSHOT_FILE, ensure_dirs

CHECKPOINT_EVERY = 25


CACHE_KEYS = (
    "tracks", "albums", "album_tracks", "resolved", "unresolvable", "searched",
)
# Bumped when a cached row gains a field, so stale rows are refetched rather than
# silently missing it. Album track titles and search results are unaffected.
CACHE_VERSION = 2


def _load_cache() -> Dict[str, Any]:
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            for key in CACHE_KEYS:
                data.setdefault(key, {})
            if data.get("version") != CACHE_VERSION:
                # Track and album rows predate a field; drop just those.
                data["tracks"] = {}
                data["albums"] = {}
                data["version"] = CACHE_VERSION
            return data
        except Exception:
            pass
    return {**{key: {} for key in CACHE_KEYS}, "version": CACHE_VERSION}


def _save_cache(cache: Dict[str, Any]) -> None:
    ensure_dirs()
    tmp = CACHE_FILE.with_suffix(".tmp")
    cache["version"] = CACHE_VERSION
    tmp.write_text(json.dumps(cache, ensure_ascii=False))
    tmp.replace(CACHE_FILE)


def build(
    session: tidalapi.Session,
    verbose: bool = True,
    workers: int = fetch.DEFAULT_WORKERS,
    force: bool = False,
    candidates: int = 3,
    resolve: bool = True,
) -> Dict[str, Any]:
    cache = _load_cache()
    if force:
        cache["tracks"] = {}

    if verbose:
        print("Listing playlists...", file=sys.stderr)
    playlists = fetch.list_owned_playlists(session, verbose=verbose)

    # --- track lists -------------------------------------------------------
    stale = [
        p
        for p in playlists
        if cache["tracks"].get(p["id"], {}).get("last_updated") != p["last_updated"]
    ]
    cached = len(playlists) - len(stale)
    if verbose:
        print(
            f"{len(playlists)} playlists; {cached} unchanged since last run, "
            f"{len(stale)} to fetch.",
            file=sys.stderr,
        )

    pending = {"n": 0}

    def store_tracks(playlist: Dict[str, Any], value: Any) -> None:
        if isinstance(value, dict) and value.get("__error__"):
            cache["tracks"][playlist["id"]] = {
                "last_updated": None,
                "tracks": [],
                "error": value["__error__"],
            }
        else:
            tracks, other = value
            cache["tracks"][playlist["id"]] = {
                "last_updated": playlist["last_updated"],
                "tracks": tracks,
                "non_track_items": other,
            }
        pending["n"] += 1
        if pending["n"] >= CHECKPOINT_EVERY:
            _save_cache(cache)
            pending["n"] = 0

    if stale:
        fetch.parallel_map(
            session,
            stale,
            lambda s, p: fetch.playlist_tracks(s, p["id"]),
            label="tracks",
            workers=workers,
            on_result=store_tracks,
            verbose=verbose,
            note=lambda p: p["name"],
        )
        _save_cache(cache)

    rows: List[Dict[str, Any]] = []
    for playlist in playlists:
        entry = cache["tracks"].get(playlist["id"], {})
        row = dict(playlist)
        row["tracks"] = entry.get("tracks") or []
        row["non_track_items"] = entry.get("non_track_items", 0)
        if entry.get("error"):
            row["fetch_error"] = entry["error"]
        rows.append(row)

    # --- album metadata and track lists ------------------------------------
    # The verdict compares track *titles* against the album's real track list, so
    # both the album record and its track titles are needed. Several candidates per
    # playlist are fetched, because the most-cited album id is often a compilation
    # a few tracks happen to be filed under rather than the album itself.
    from .classify import all_candidate_album_ids

    wanted: Set[str] = all_candidate_album_ids(rows, limit=candidates)

    def _checkpoint() -> None:
        pending["n"] += 1
        if pending["n"] >= CHECKPOINT_EVERY:
            _save_cache(cache)
            pending["n"] = 0

    def store_album(album_id: str, value: Any) -> None:
        if isinstance(value, dict) and value.get("__error__"):
            cache["albums"][album_id] = {"id": album_id, "error": value["__error__"]}
        else:
            cache["albums"][album_id] = value
        _checkpoint()

    def store_titles(album_id: str, value: Any) -> None:
        if isinstance(value, dict) and value.get("__error__"):
            cache["album_tracks"][album_id] = []
        else:
            cache["album_tracks"][album_id] = value
        _checkpoint()

    missing = sorted(a for a in wanted if a not in cache["albums"])
    if verbose:
        print(
            f"{len(wanted)} candidate album(s); {len(wanted) - len(missing)} cached, "
            f"{len(missing)} to fetch.",
            file=sys.stderr,
        )
    if missing:
        fetch.parallel_map(
            session, missing, lambda s, a: fetch.album_row(s, a),
            label="album info", workers=workers, on_result=store_album, verbose=verbose,
        )
        _save_cache(cache)

    # Only ask for track lists where the album itself resolved.
    need_titles = sorted(
        a
        for a in wanted
        if a not in cache["album_tracks"] and not (cache["albums"].get(a) or {}).get("error")
    )
    if need_titles:
        if verbose:
            print(f"{len(need_titles)} album track list(s) to fetch.", file=sys.stderr)
        fetch.parallel_map(
            session, need_titles, lambda s, a: fetch.album_track_titles(s, a),
            label="album tracks", workers=workers, on_result=store_titles, verbose=verbose,
        )
        _save_cache(cache)

    snapshot = _assemble(session, rows, cache, wanted)
    if _revalidate_resolved(snapshot, cache, verbose=verbose):
        _save_cache(cache)
        snapshot = _assemble(session, rows, cache, wanted)

    if resolve:
        found = resolve_unavailable(session, snapshot, cache, verbose=verbose)
        if found:
            _save_cache(cache)
            snapshot = _assemble(session, rows, cache, wanted)
        found = resolve_by_name(session, snapshot, cache, verbose=verbose)
        if found:
            _save_cache(cache)
            snapshot = _assemble(session, rows, cache, wanted)
    return snapshot


def _assemble(
    session: tidalapi.Session,
    rows: List[Dict[str, Any]],
    cache: Dict[str, Any],
    wanted: Set[str],
) -> Dict[str, Any]:
    keys = set(wanted) | set(cache["resolved"].values())
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "user": {"id": session.user.id, "username": getattr(session.user, "username", None)},
        "playlists": rows,
        "albums": {a: cache["albums"][a] for a in keys if a in cache["albums"]},
        "album_tracks": {
            a: cache["album_tracks"][a] for a in keys if a in cache["album_tracks"]
        },
        "resolved": dict(cache["resolved"]),
    }


def resolve_unavailable(
    session: tidalapi.Session,
    snapshot: Dict[str, Any],
    cache: Dict[str, Any],
    verbose: bool = True,
) -> int:
    """Re-find albums that have been delisted from Tidal, by search.

    When an album is pulled from the catalogue its id 404s — the album, and even
    its tracks — while playlists keep referencing it. The album usually still
    exists under a new id, so search for it by name and accept a hit only if its
    track titles actually match the playlist's.
    """
    from .classify import artist_agrees, classify_all, score_against, title_set

    entries = classify_all(snapshot)
    playlists = {p["id"]: p for p in snapshot["playlists"]}
    todo = [
        e
        for e in entries
        if e["verdict"] == "unresolved"
        and e["playlist_id"] not in cache["resolved"]
        and e["playlist_id"] not in cache["unresolvable"]
    ]
    if not todo:
        return 0
    if verbose:
        print(
            f"Re-finding {len(todo)} album(s) that no longer resolve on Tidal...",
            file=sys.stderr,
        )

    found = 0
    progress = fetch.Progress(len(todo), "resolving", enabled=verbose)
    for entry in todo:
        guess = entry.get("album_guess") or {}
        query = " ".join(x for x in (guess.get("artist"), guess.get("name")) if x)
        playlist_titles = title_set(
            t.get("name") for t in playlists[entry["playlist_id"]]["tracks"]
        )
        best = None
        if query:
            try:
                for hit in fetch.search_albums(session, query, limit=5):
                    if not artist_agrees(
                        playlists[entry["playlist_id"]]["tracks"],
                        entry["name"],
                        hit.get("artist"),
                    ):
                        continue
                    titles = cache["album_tracks"].get(hit["id"])
                    if titles is None:
                        try:
                            titles = fetch.album_track_titles(session, hit["id"])
                        except Exception:
                            titles = []
                        cache["album_tracks"][hit["id"]] = titles
                    score = score_against(playlist_titles, title_set(titles))
                    # Accept only a near-equal album: nothing in the playlist may
                    # be absent from it, and it must be mostly covered.
                    if score["precision"] >= 0.9 and score["recall"] >= 0.6:
                        if best is None or score["f1"] > best[1]["f1"]:
                            best = (hit["id"], score)
            except Exception:
                best = None

        if best:
            album_id = best[0]
            if album_id not in cache["albums"] or cache["albums"][album_id].get("error"):
                try:
                    row = fetch.album_row(session, album_id)
                except Exception:
                    row = None
                if row:
                    row["resolved_from"] = guess.get("id")
                    cache["albums"][album_id] = row
            cache["resolved"][entry["playlist_id"]] = album_id
            found += 1
        else:
            cache["unresolvable"][entry["playlist_id"]] = guess.get("name") or ""
        progress.tick(entry["name"])
    progress.close()
    if verbose:
        print(
            f"Recovered {found} of {len(todo)}; {len(todo) - found} could not be found.",
            file=sys.stderr,
        )
    return found


def save(snapshot: Dict[str, Any]) -> None:
    ensure_dirs()
    SNAPSHOT_FILE.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))


def load() -> Dict[str, Any]:
    if not SNAPSHOT_FILE.exists():
        raise SystemExit("No snapshot yet. Run `tidalcleanup audit` first.")
    return json.loads(SNAPSHOT_FILE.read_text())


def drop(playlist_ids: Set[str]) -> None:
    """Forget playlists that no longer exist, so the snapshot stays truthful."""
    if not playlist_ids:
        return
    cache = _load_cache()
    for pid in playlist_ids:
        cache["tracks"].pop(pid, None)
    _save_cache(cache)
    if SNAPSHOT_FILE.exists():
        snap = load()
        snap["playlists"] = [
            p for p in snap["playlists"] if p["id"] not in playlist_ids
        ]
        save(snap)


def load_or_build(
    session: tidalapi.Session,
    refresh: bool,
    workers: int = fetch.DEFAULT_WORKERS,
    resolve: bool = True,
) -> Dict[str, Any]:
    if refresh or not SNAPSHOT_FILE.exists():
        started = time.monotonic()
        snap = build(session, workers=workers, resolve=resolve)
        save(snap)
        print(
            f"Snapshot built in {int(time.monotonic() - started)}s "
            f"({len(snap['playlists'])} playlists).",
            file=sys.stderr,
        )
        return snap
    return load()


# Promotion out of "curated" demands a near-exact album: nothing in the playlist
# may be missing from it, and it must be almost entirely covered.
PROMOTE_PRECISION = 0.90
PROMOTE_RECALL = 0.85


def _verify_hit(
    session: tidalapi.Session,
    cache: Dict[str, Any],
    hit: Dict[str, Any],
    playlist_titles: Set[str],
    track_count: int,
) -> Optional[Dict[str, float]]:
    """Score one search hit, fetching its track list only if the size is plausible."""
    from .classify import score_against, title_set

    size = hit.get("num_tracks")
    if size:
        # We want an album the playlist nearly *equals*; skip wildly different sizes
        # before spending a request on its track list.
        ratio = track_count / size
        if not 0.55 <= ratio <= 1.8:
            return None
    titles = cache["album_tracks"].get(hit["id"])
    if titles is None:
        try:
            titles = fetch.album_track_titles(session, hit["id"])
        except Exception:
            titles = []
        cache["album_tracks"][hit["id"]] = titles
    if not titles:
        return None
    return score_against(playlist_titles, title_set(titles))


def _adopt(
    session: tidalapi.Session,
    cache: Dict[str, Any],
    playlist_id: str,
    album_id: str,
    dead_id: Optional[str] = None,
) -> None:
    if album_id not in cache["albums"] or cache["albums"][album_id].get("error"):
        try:
            row = fetch.album_row(session, album_id)
        except Exception:
            return
        if dead_id:
            row["resolved_from"] = dead_id
        cache["albums"][album_id] = row
    cache["resolved"][playlist_id] = album_id


def resolve_by_name(
    session: tidalapi.Session,
    snapshot: Dict[str, Any],
    cache: Dict[str, Any],
    verbose: bool = True,
) -> int:
    """Search by playlist title for playlists whose tracks don't cite the real album.

    A Spotify import can leave every track filed under a compilation or a different
    edition, so the album the playlist actually *is* never appears among the ids its
    tracks reference. Searching the playlist's own name finds it; a hit is adopted
    only if its track titles nearly equal the playlist's.
    """
    from .classify import artist_agrees, classify_all, title_set

    entries = classify_all(snapshot)
    playlists = {p["id"]: p for p in snapshot["playlists"]}
    todo = [
        e
        for e in entries
        if e["verdict"] not in ("album", "likely_album", "empty", "too_small")
        and e["num_tracks"] >= 3
        and e["playlist_id"] not in cache["resolved"]
        and e["playlist_id"] not in cache["searched"]
    ]
    if not todo:
        return 0
    if verbose:
        print(
            f"Searching by playlist name for {len(todo)} unmatched playlist(s)...",
            file=sys.stderr,
        )

    found = 0
    progress = fetch.Progress(len(todo), "name search", enabled=verbose)
    for entry in todo:
        cache["searched"][entry["playlist_id"]] = True
        titles = title_set(
            t.get("name") for t in playlists[entry["playlist_id"]]["tracks"]
        )
        best: Optional[Any] = None
        try:
            hits = fetch.search_albums(session, entry["name"], limit=5)
        except Exception:
            hits = []
        for hit in hits:
            if not artist_agrees(
                playlists[entry["playlist_id"]]["tracks"], entry["name"], hit.get("artist")
            ):
                continue
            score = _verify_hit(session, cache, hit, titles, entry["num_tracks"])
            if not score:
                continue
            if (
                score["precision"] >= PROMOTE_PRECISION
                and score["recall"] >= PROMOTE_RECALL
                and (best is None or score["f1"] > best[1]["f1"])
            ):
                best = (hit["id"], score)
        if best:
            _adopt(session, cache, entry["playlist_id"], best[0])
            found += 1
        progress.tick(entry["name"])
    progress.close()
    if verbose:
        print(f"Matched {found} more playlist(s) by name.", file=sys.stderr)
    return found


def _revalidate_resolved(
    snapshot: Dict[str, Any], cache: Dict[str, Any], verbose: bool = True
) -> int:
    """Drop stored matches that no longer pass the artist check. Local, no requests."""
    from .classify import artist_agrees

    playlists = {p["id"]: p for p in snapshot["playlists"]}
    dropped = 0
    for playlist_id, album_id in list(cache["resolved"].items()):
        playlist = playlists.get(playlist_id)
        album = (snapshot.get("albums") or {}).get(album_id)
        if not playlist or not album:
            continue
        if not artist_agrees(
            playlist.get("tracks") or [], playlist["name"], album.get("artist")
        ):
            cache["resolved"].pop(playlist_id, None)
            cache["searched"][playlist_id] = True
            dropped += 1
            if verbose:
                print(
                    f"  dropping match for {playlist['name']!r}: album artist "
                    f"{album.get('artist')!r} doesn't match the playlist",
                    file=sys.stderr,
                )
    return dropped


def patch(updates: Dict[str, Dict[str, Any]]) -> None:
    """Apply field changes to the stored snapshot so it stays truthful.

    After flipping visibility or clearing notes, the local copy would otherwise
    still describe the old state and a re-run would reissue redundant writes.
    """
    if not updates or not SNAPSHOT_FILE.exists():
        return
    snap = load()
    for playlist in snap.get("playlists") or []:
        change = updates.get(playlist["id"])
        if change:
            playlist.update(change)
    save(snap)

    cache = _load_cache()
    touched = False
    for playlist_id, change in updates.items():
        entry = cache["tracks"].get(playlist_id)
        if entry and "last_updated" in change:
            entry["last_updated"] = change["last_updated"]
            touched = True
    if touched:
        _save_cache(cache)
