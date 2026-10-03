"""Raw Tidal endpoints, paginated and parallel.

tidalapi's own helpers are unusable at a thousand playlists: `user.playlists()`
sends no limit/offset (Tidal caps the page at 50, so it silently returns only the
first page) and its playlist parser constructs a `UserPlaylist` per row, which
fires an extra GET each. So this module talks to the JSON endpoints directly.
"""

from __future__ import annotations

import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import tidalapi

from .paths import SESSION_FILE

PLAYLIST_PAGE = 50   # Tidal's hard cap on the playlist-listing endpoints
ITEM_PAGE = 100      # tracks per request
MAX_RETRIES = 8
DEFAULT_WORKERS = 6

# requests.Session is not thread-safe, so each worker thread gets its own
# tidalapi.Session loaded from the same stored credentials.
_local = threading.local()
_print_lock = threading.Lock()

# Counts how long we have spent waiting out Tidal rate limits, so a throttled run
# reports "waiting" rather than looking frozen.
THROTTLE = {"hits": 0, "seconds": 0.0}


def thread_session(primary: tidalapi.Session) -> tidalapi.Session:
    if getattr(_local, "session", None) is None:
        if threading.current_thread() is threading.main_thread():
            _local.session = primary
        else:
            clone = tidalapi.Session()
            clone.load_session_from_file(SESSION_FILE)
            _local.session = clone
    return _local.session


def _is_rate_limit(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in ("TooManyRequests", "TooManyRequestsError"):
        return True
    text = str(exc)
    return "429" in text or "Too Many Requests" in text


def _is_missing(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in ("ObjectNotFound", "NotFound"):
        return True
    return "404" in str(exc)


def get_json(
    session: tidalapi.Session,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """GET with backoff. Rate limits and transient 5xx are retried; 404 is raised."""
    delay = 1.0
    last: Optional[BaseException] = None
    for attempt in range(MAX_RETRIES):
        try:
            response = session.request.request(
                "GET", path, params=params, base_url=base_url
            )
            return response.json()
        except Exception as exc:  # noqa: BLE001 - tidalapi raises a mix of types
            last = exc
            if _is_missing(exc):
                raise
            if attempt == MAX_RETRIES - 1:
                break
            if _is_rate_limit(exc):
                wait = min(max(delay * 4, 5.0), 15.0)
                THROTTLE["hits"] += 1
                THROTTLE["seconds"] += wait
            elif "50" in str(exc)[:200] or "timeout" in str(exc).lower():
                wait = delay
            else:
                raise
            time.sleep(wait + random.uniform(0, 0.4))
            delay = min(delay * 2, 30)
    raise RuntimeError(f"GET {path} failed after {MAX_RETRIES} tries: {last}")


# ------------------------------------------------------------------ listing

def list_owned_playlists(session: tidalapi.Session, verbose: bool = True) -> List[Dict[str, Any]]:
    """Every playlist the logged-in user created, across all folders.

    Uses playlistsAndFavoritePlaylists (which does accept limit/offset) and keeps
    only rows whose creator is the logged-in user, so playlists you merely follow
    are never returned — we must not be able to delete someone else's playlist.
    """
    user_id = str(session.user.id)
    rows: Dict[str, Dict[str, Any]] = {}
    followed = 0
    offset = 0
    total: Optional[int] = None

    while True:
        payload = get_json(
            session,
            f"users/{user_id}/playlistsAndFavoritePlaylists",
            params={"limit": PLAYLIST_PAGE, "offset": offset},
        )
        items = payload.get("items") or []
        if total is None:
            total = payload.get("totalNumberOfItems")
        for item in items:
            playlist = item.get("playlist", item)
            creator = playlist.get("creator") or {}
            uuid = playlist.get("uuid")
            if not uuid:
                continue
            if str(creator.get("id")) != user_id:
                followed += 1
                continue
            rows[uuid] = {
                "id": uuid,
                "name": playlist.get("title") or "(untitled)",
                "description": playlist.get("description") or "",
                "created": playlist.get("created"),
                "last_updated": playlist.get("lastUpdated"),
                "num_tracks": int(playlist.get("numberOfTracks") or 0),
                "num_videos": int(playlist.get("numberOfVideos") or 0),
                "duration": playlist.get("duration"),
                "type": playlist.get("type"),
                "public": playlist.get("publicPlaylist"),
                "share_url": f"https://tidal.com/playlist/{uuid}",
            }
        if verbose:
            print(
                f"  listed {len(rows)} own playlists"
                + (f" of {total} entries" if total else ""),
                file=sys.stderr,
            )
        offset += PLAYLIST_PAGE
        if len(items) < PLAYLIST_PAGE:
            break
        if total and offset >= total:
            break
        if offset > 20000:  # refuse to loop forever on a misbehaving endpoint
            break

    if verbose and followed:
        print(f"  ignoring {followed} playlist(s) you follow but don't own", file=sys.stderr)
    return list(rows.values())


# ------------------------------------------------------------------ tracks

def _track_row(track: Dict[str, Any]) -> Dict[str, Any]:
    album = track.get("album") or {}
    artist = track.get("artist") or {}
    artists = track.get("artists") or []
    return {
        "id": str(track.get("id")),
        "name": track.get("title"),
        "artist": artist.get("name"),
        "artists": [a.get("name") for a in artists if a.get("name")],
        "album_id": str(album["id"]) if album.get("id") is not None else None,
        "album_name": album.get("title"),
        # A track's nested album carries no artist, so fall back to the performer.
        "album_artist": (album.get("artist") or {}).get("name") or artist.get("name"),
        "track_num": track.get("trackNumber"),
        "volume_num": track.get("volumeNumber"),
        "isrc": track.get("isrc"),
        "duration": track.get("duration"),
        "audio_modes": track.get("audioModes") or [],
        "media_tags": (track.get("mediaMetadata") or {}).get("tags") or [],
    }


def playlist_tracks(session: tidalapi.Session, playlist_id: str) -> Tuple[List[Dict[str, Any]], int]:
    """All track rows for one playlist, plus a count of non-track items."""
    rows: List[Dict[str, Any]] = []
    other = 0
    offset = 0
    while True:
        payload = get_json(
            session,
            f"playlists/{playlist_id}/items",
            params={"limit": ITEM_PAGE, "offset": offset},
        )
        items = payload.get("items") or []
        for entry in items:
            item = entry.get("item", entry)
            kind = (entry.get("type") or item.get("type") or "").upper()
            if kind and kind != "TRACK":
                other += 1
                continue
            if item.get("id") is None:
                other += 1
                continue
            rows.append(_track_row(item))
        offset += ITEM_PAGE
        if len(items) < ITEM_PAGE:
            break
        total = payload.get("totalNumberOfItems")
        if total and offset >= total:
            break
    return rows, other


def album_row(session: tidalapi.Session, album_id: str) -> Dict[str, Any]:
    payload = get_json(session, f"albums/{album_id}")
    artist = payload.get("artist") or {}
    return {
        "id": str(payload.get("id")),
        "name": payload.get("title"),
        "artist": artist.get("name"),
        "artist_id": str(artist["id"]) if artist.get("id") is not None else None,
        "artists": [a.get("name") for a in (payload.get("artists") or []) if a.get("name")],
        "num_tracks": payload.get("numberOfTracks"),
        "num_volumes": payload.get("numberOfVolumes"),
        "release_date": payload.get("releaseDate"),
        "type": payload.get("type"),
        "upc": payload.get("upc"),
        "audio_modes": payload.get("audioModes") or [],
        "media_tags": (payload.get("mediaMetadata") or {}).get("tags") or [],
        "audio_quality": payload.get("audioQuality"),
        "share_url": f"https://tidal.com/album/{payload.get('id')}",
    }


# ------------------------------------------------------------------ parallel map

class Progress:
    """Single-line progress with a throughput-based ETA."""

    def __init__(self, total: int, label: str, enabled: bool = True):
        self.total = total
        self.label = label
        self.enabled = enabled and total > 0
        self.done = 0
        self.started = time.monotonic()

    def tick(self, note: str = "") -> None:
        if not self.enabled:
            return
        self.done += 1
        elapsed = time.monotonic() - self.started
        rate = self.done / elapsed if elapsed > 0 else 0
        remaining = (self.total - self.done) / rate if rate > 0 else 0
        mins, secs = divmod(int(remaining), 60)
        throttled = ""
        if THROTTLE["hits"]:
            throttled = (
                f" [rate-limited {THROTTLE['hits']}x, "
                f"waited {int(THROTTLE['seconds'])}s]"
            )
        with _print_lock:
            sys.stderr.write(
                f"\r  {self.label} {self.done}/{self.total} "
                f"({rate:4.1f}/s, ~{mins}m{secs:02d}s left){throttled} {note[:32]:<32}"
            )
            sys.stderr.flush()

    def close(self) -> None:
        if self.enabled:
            elapsed = int(time.monotonic() - self.started)
            with _print_lock:
                sys.stderr.write(
                    f"\r  {self.label} {self.done}/{self.total} done in {elapsed}s"
                    + " " * 40
                    + "\n"
                )
                sys.stderr.flush()


def parallel_map(
    session: tidalapi.Session,
    work: Iterable[Any],
    fn: Callable[[tidalapi.Session, Any], Any],
    label: str,
    workers: int = DEFAULT_WORKERS,
    on_result: Optional[Callable[[Any, Any], None]] = None,
    verbose: bool = True,
    note: Optional[Callable[[Any], str]] = None,
) -> Dict[int, Any]:
    """Run fn over work with a thread pool, reporting progress and surviving errors.

    on_result is called on the main loop as each item lands, which is what lets the
    caller checkpoint partial results to disk.
    """
    work = list(work)
    results: Dict[int, Any] = {}
    progress = Progress(len(work), label, enabled=verbose)

    def run(index: int, item: Any):
        return index, fn(thread_session(session), item)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(run, i, item): (i, item) for i, item in enumerate(work)}
        for future in as_completed(futures):
            index, item = futures[future]
            try:
                _, value = future.result()
                results[index] = value
                if on_result:
                    on_result(item, value)
            except Exception as exc:  # keep going; the caller records the gap
                results[index] = {"__error__": str(exc)}
                if on_result:
                    on_result(item, {"__error__": str(exc)})
            progress.tick(note(item) if note else "")
    progress.close()
    return results


# ------------------------------------------------------------------ search

def search_albums(
    session: tidalapi.Session, query: str, limit: int = 5
) -> List[Dict[str, Any]]:
    """Album hits for a free-text query, used to re-find delisted albums."""
    payload = get_json(
        session, "search", params={"query": query, "limit": limit, "types": "ALBUMS"}
    )
    hits = ((payload.get("albums") or {}).get("items")) or []
    out = []
    for hit in hits:
        if hit.get("id") is None:
            continue
        artist = hit.get("artist") or {}
        artists = hit.get("artists") or []
        out.append(
            {
                "id": str(hit["id"]),
                "name": hit.get("title"),
                "artist": artist.get("name") or (artists[0].get("name") if artists else None),
                "num_tracks": hit.get("numberOfTracks"),
            }
        )
    return out


def album_track_titles(session: tidalapi.Session, album_id: str) -> List[str]:
    titles: List[str] = []
    offset = 0
    while True:
        payload = get_json(
            session, f"albums/{album_id}/items", params={"limit": ITEM_PAGE, "offset": offset}
        )
        items = payload.get("items") or []
        for entry in items:
            item = entry.get("item", entry)
            if item.get("title"):
                titles.append(item["title"])
        offset += ITEM_PAGE
        total = payload.get("totalNumberOfItems")
        if len(items) < ITEM_PAGE or (total and offset >= total):
            break
    return titles


def favorite_albums(
    session: tidalapi.Session, verbose: bool = True
) -> List[Dict[str, Any]]:
    """Every saved album, with full metadata, from the favourites listing itself.

    The favourites payload already embeds artist, audioModes, mediaMetadata and
    numberOfTracks for each album, so there is no reason to fetch albums one by
    one: 756 albums come back in ~16 requests instead of 756.
    """
    user_id = str(session.user.id)
    rows: Dict[str, Dict[str, Any]] = {}
    offset = 0
    while True:
        payload = get_json(
            session,
            f"users/{user_id}/favorites/albums",
            params={"limit": 50, "offset": offset},
        )
        items = payload.get("items") or []
        for entry in items:
            album = entry.get("item", entry)
            if album.get("id") is None:
                continue
            artist = album.get("artist") or {}
            rows[str(album["id"])] = {
                "id": str(album["id"]),
                "name": album.get("title"),
                "artist": artist.get("name"),
                "artist_id": str(artist["id"]) if artist.get("id") is not None else None,
                "artists": [a.get("name") for a in (album.get("artists") or []) if a.get("name")],
                "num_tracks": album.get("numberOfTracks"),
                "num_volumes": album.get("numberOfVolumes"),
                "release_date": album.get("releaseDate"),
                "type": album.get("type"),
                "upc": album.get("upc"),
                "audio_modes": album.get("audioModes") or [],
                "media_tags": (album.get("mediaMetadata") or {}).get("tags") or [],
                "audio_quality": album.get("audioQuality"),
                "share_url": f"https://tidal.com/album/{album['id']}",
            }
        offset += 50
        total = payload.get("totalNumberOfItems")
        if verbose:
            print(f"  read {len(rows)} saved album(s)", file=sys.stderr)
        if len(items) < 50 or (total and offset >= total) or offset > 100000:
            break
    return list(rows.values())
