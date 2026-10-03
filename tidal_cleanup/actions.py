"""The side-effecting half: back up, favourite, delete, restore.

Batch shape matters at a thousand playlists. Favourites accept a comma-separated
list, so saving 847 albums is ~17 requests rather than 847. Deleting is still one
request each, but it only happens after the whole batch of albums is confirmed
present in the collection.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import tidalapi

from . import fetch
from .paths import ACTION_LOG, BACKUP_DIR, IGNORE_FILE, ensure_dirs

FAVORITE_BATCH = 50


# ----------------------------------------------------------------- bookkeeping

def _slug(text: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Z]+", "-", text).strip("-").lower()
    return (slug or "playlist")[:60]


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup_playlist(playlist: Dict[str, Any], directory: Optional[Path] = None) -> Path:
    """Write the full playlist (metadata + every track) to disk before touching it.

    This is what `restore` reads, so it happens unconditionally and up front.
    """
    target = directory or BACKUP_DIR
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{_slug(playlist['name'])}-{playlist['id']}.json"
    path.write_text(
        json.dumps(
            {"backed_up_at": _stamp(), "playlist": playlist}, indent=2, ensure_ascii=False
        )
    )
    return path


def backup_batch(playlists: Sequence[Dict[str, Any]]) -> Tuple[Path, List[Path]]:
    ensure_dirs()
    directory = BACKUP_DIR / f"{_stamp()}-batch"
    paths = [backup_playlist(p, directory) for p in playlists]
    return directory, paths


def log_action(record: Dict[str, Any]) -> None:
    ensure_dirs()
    with ACTION_LOG.open("a") as handle:
        handle.write(
            json.dumps(
                {"at": datetime.now(timezone.utc).isoformat(), **record},
                ensure_ascii=False,
            )
            + "\n"
        )


def load_ignored() -> Set[str]:
    if not IGNORE_FILE.exists():
        return set()
    try:
        return set(json.loads(IGNORE_FILE.read_text()).get("playlist_ids", []))
    except Exception:
        return set()


def add_ignored(playlist_id: str, name: str) -> None:
    ensure_dirs()
    data: Dict[str, Any] = {"playlist_ids": [], "names": {}}
    if IGNORE_FILE.exists():
        try:
            data = json.loads(IGNORE_FILE.read_text())
            data.setdefault("playlist_ids", [])
            data.setdefault("names", {})
        except Exception:
            pass
    if playlist_id not in data["playlist_ids"]:
        data["playlist_ids"].append(playlist_id)
    data["names"][playlist_id] = name
    IGNORE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ----------------------------------------------------------------- collection

class Collection:
    """The user's saved albums and followed artists."""

    def __init__(self, session: tidalapi.Session):
        self.session = session
        self.user_id = str(session.user.id)
        self._albums: Optional[Set[str]] = None
        self._artists: Optional[Set[str]] = None

    def _favorite_ids(self, kind: str) -> Set[str]:
        """Page the whole favourites list; Tidal caps these at 50 per request."""
        ids: Set[str] = set()
        offset = 0
        while True:
            payload = fetch.get_json(
                self.session,
                f"users/{self.user_id}/favorites/{kind}",
                params={"limit": 50, "offset": offset},
            )
            items = payload.get("items") or []
            for entry in items:
                item = entry.get("item", entry)
                if item.get("id") is not None:
                    ids.add(str(item["id"]))
            offset += 50
            total = payload.get("totalNumberOfItems")
            if len(items) < 50 or (total and offset >= total):
                break
            if offset > 100000:
                break
        return ids

    @property
    def album_ids(self) -> Set[str]:
        if self._albums is None:
            self._albums = self._favorite_ids("albums")
        return self._albums

    @property
    def artist_ids(self) -> Set[str]:
        if self._artists is None:
            self._artists = self._favorite_ids("artists")
        return self._artists

    def refresh_albums(self) -> Set[str]:
        self._albums = self._favorite_ids("albums")
        return self._albums

    def has_album(self, album_id: str) -> bool:
        return str(album_id) in self.album_ids

    def add_albums(self, album_ids: Sequence[str]) -> List[str]:
        """POST albums in batches. Returns ids whose request failed outright."""
        todo = [str(a) for a in album_ids if str(a) not in self.album_ids]
        failed: List[str] = []
        for chunk in _chunks(todo, FAVORITE_BATCH):
            try:
                self.session.user.favorites.add_album(list(chunk))
            except Exception:
                # Retry singly so one bad id can't sink the whole chunk.
                for album_id in chunk:
                    try:
                        self.session.user.favorites.add_album(album_id)
                    except Exception:
                        failed.append(album_id)
        self._albums = None
        return failed

    def add_artists(self, artist_ids: Sequence[str]) -> List[str]:
        todo = [str(a) for a in artist_ids if str(a) not in self.artist_ids]
        failed: List[str] = []
        for chunk in _chunks(todo, FAVORITE_BATCH):
            try:
                self.session.user.favorites.add_artist(list(chunk))
            except Exception:
                for artist_id in chunk:
                    try:
                        self.session.user.favorites.add_artist(artist_id)
                    except Exception:
                        failed.append(artist_id)
        self._artists = None
        return failed

    # Kept for the one-at-a-time review loop.
    def add_album(self, album_id: str) -> bool:
        album_id = str(album_id)
        if album_id in self.album_ids:
            return True
        self.session.user.favorites.add_album(album_id)
        self._albums = None
        return album_id in self.album_ids

    def add_artist(self, artist_id: str) -> bool:
        artist_id = str(artist_id)
        if artist_id in self.artist_ids:
            return True
        self.session.user.favorites.add_artist(artist_id)
        self._artists = None
        return artist_id in self.artist_ids


def delete_playlist(session: tidalapi.Session, playlist_id: str) -> bool:
    """DELETE straight to the endpoint; UserPlaylist's constructor would add a GET."""
    return session.request.request("DELETE", f"playlists/{playlist_id}").ok


# ----------------------------------------------------------------- staleness

def stale_playlists(
    session: tidalapi.Session, playlists: Sequence[Dict[str, Any]], verbose: bool = True
) -> Tuple[Set[str], Set[str]]:
    """Compare the snapshot against Tidal right now.

    Returns (changed, vanished). A playlist edited since the audit is excluded from
    the batch: its snapshot — and so the backup and the album match — is out of date,
    and deleting it could discard tracks added in the meantime.
    """
    if verbose:
        print("Re-checking playlists for changes since the audit...", file=sys.stderr)
    live = {p["id"]: p for p in fetch.list_owned_playlists(session, verbose=False)}
    changed: Set[str] = set()
    vanished: Set[str] = set()
    for playlist in playlists:
        current = live.get(playlist["id"])
        if current is None:
            vanished.add(playlist["id"])
        elif current.get("last_updated") != playlist.get("last_updated"):
            changed.add(playlist["id"])
    return changed, vanished


# ----------------------------------------------------------------- apply

def apply_batch(
    session: tidalapi.Session,
    collection: Collection,
    entries: Sequence[Dict[str, Any]],
    playlists: Dict[str, Dict[str, Any]],
    add_artist: bool = True,
    delete: bool = True,
    dry_run: bool = False,
    workers: int = 4,
    verbose: bool = True,
    check_stale: bool = True,
) -> Dict[str, Any]:
    """Save every matched album, then delete the playlists that are now redundant."""
    summary: Dict[str, Any] = {
        "requested": len(entries),
        "skipped_no_album": 0,
        "skipped_stale": [],
        "skipped_vanished": [],
        "albums_saved": 0,
        "artists_saved": 0,
        "deleted": 0,
        "delete_failed": [],
        "albums_unconfirmed": [],
        "backup_dir": None,
        "dry_run": dry_run,
    }

    usable = []
    for entry in entries:
        if not (entry.get("album") or {}).get("id"):
            summary["skipped_no_album"] += 1
            continue
        usable.append(entry)

    if check_stale and usable and not dry_run:
        changed, vanished = stale_playlists(
            session, [playlists[e["playlist_id"]] for e in usable], verbose=verbose
        )
        if changed or vanished:
            summary["skipped_stale"] = sorted(changed)
            summary["skipped_vanished"] = sorted(vanished)
            usable = [
                e
                for e in usable
                if e["playlist_id"] not in changed and e["playlist_id"] not in vanished
            ]

    if not usable:
        return summary

    album_ids = sorted({str(e["album"]["id"]) for e in usable})
    artist_ids = sorted(
        {str(e["album"]["artist_id"]) for e in usable if e["album"].get("artist_id")}
    )

    if dry_run:
        summary["albums_saved"] = len([a for a in album_ids if not collection.has_album(a)])
        summary["artists_saved"] = len(artist_ids) if add_artist else 0
        summary["deleted"] = len(usable) if delete else 0
        summary["would_convert"] = [e["name"] for e in usable]
        return summary

    # 1. Back up everything first, before a single request goes out.
    directory, _ = backup_batch([playlists[e["playlist_id"]] for e in usable])
    summary["backup_dir"] = str(directory)
    if verbose:
        print(f"Backed up {len(usable)} playlist(s) to {directory}", file=sys.stderr)

    # 2. Save the albums (and artists).
    before = set(collection.album_ids)
    if verbose:
        print(f"Saving {len(album_ids)} album(s)...", file=sys.stderr)
    collection.add_albums(album_ids)
    if add_artist and artist_ids:
        if verbose:
            print(f"Following {len(artist_ids)} artist(s)...", file=sys.stderr)
        collection.add_artists(artist_ids)

    # 3. Re-read the collection once and only trust what is actually there.
    confirmed = collection.refresh_albums()
    summary["albums_saved"] = len(confirmed - before)
    summary["albums_unconfirmed"] = sorted(a for a in album_ids if a not in confirmed)
    if add_artist:
        summary["artists_saved"] = len([a for a in artist_ids if a in collection.artist_ids])

    # 4. Delete only the playlists whose album is provably saved.
    if not delete:
        for entry in usable:
            log_action(
                {
                    "action": "save_only",
                    "playlist_id": entry["playlist_id"],
                    "playlist_name": entry["name"],
                    "album_id": entry["album"]["id"],
                    "backup_dir": str(directory),
                }
            )
        return summary

    deletable = [e for e in usable if str(e["album"]["id"]) in confirmed]
    if verbose:
        held = len(usable) - len(deletable)
        if held:
            print(
                f"Keeping {held} playlist(s) whose album did not land in the collection.",
                file=sys.stderr,
            )
        print(f"Deleting {len(deletable)} playlist(s)...", file=sys.stderr)

    def do_delete(worker_session: tidalapi.Session, entry: Dict[str, Any]) -> bool:
        return delete_playlist(worker_session, entry["playlist_id"])

    results = fetch.parallel_map(
        session,
        deletable,
        do_delete,
        label="deleting",
        workers=workers,
        verbose=verbose,
        note=lambda e: e["name"],
    )

    for index, entry in enumerate(deletable):
        value = results.get(index)
        ok = value is True
        if ok:
            summary["deleted"] += 1
        else:
            summary["delete_failed"].append(
                {
                    "playlist_id": entry["playlist_id"],
                    "name": entry["name"],
                    "error": value.get("__error__") if isinstance(value, dict) else str(value),
                }
            )
        log_action(
            {
                "action": "convert",
                "playlist_id": entry["playlist_id"],
                "playlist_name": entry["name"],
                "album_id": entry["album"]["id"],
                "album_name": entry["album"].get("name"),
                "tier": entry.get("tier"),
                "playlist_deleted": ok,
                "backup_dir": str(directory),
            }
        )

    summary["deleted_ids"] = {
        e["playlist_id"]
        for index, e in enumerate(deletable)
        if results.get(index) is True
    }
    return summary


def convert_one(
    session: tidalapi.Session,
    collection: Collection,
    playlist: Dict[str, Any],
    entry: Dict[str, Any],
    add_artist: bool = True,
    delete: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Single-playlist path used by the interactive review loop."""
    album = entry.get("album") or {}
    outcome: Dict[str, Any] = {
        "playlist_id": playlist["id"],
        "playlist_name": playlist["name"],
        "album_id": album.get("id"),
        "album_name": album.get("name"),
        "album_saved": False,
        "artist_saved": False,
        "playlist_deleted": False,
        "backup": None,
        "dry_run": dry_run,
    }
    if not album.get("id"):
        outcome["error"] = "no album matched"
        return outcome

    if dry_run:
        outcome["album_saved"] = collection.has_album(album["id"])
        return outcome

    outcome["backup"] = str(backup_playlist(playlist))
    outcome["album_saved"] = collection.add_album(album["id"])

    if add_artist and album.get("artist_id"):
        try:
            outcome["artist_saved"] = collection.add_artist(album["artist_id"])
        except Exception as exc:
            outcome["artist_error"] = str(exc)

    if delete:
        if not outcome["album_saved"]:
            outcome["error"] = "album did not land in the collection; playlist kept"
        else:
            try:
                outcome["playlist_deleted"] = delete_playlist(session, playlist["id"])
            except Exception as exc:
                outcome["error"] = f"delete failed: {exc}"

    log_action({"action": "convert", **outcome})
    return outcome


# ----------------------------------------------------------------- restore

def list_backups() -> List[Path]:
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.rglob("*.json"))


def restore(session: tidalapi.Session, backup_path: Path) -> Dict[str, Any]:
    """Recreate a playlist from a backup file."""
    data = json.loads(Path(backup_path).read_text())
    playlist = data["playlist"]
    created = session.user.create_playlist(
        playlist["name"], playlist.get("description") or ""
    )
    track_ids = [t["id"] for t in playlist.get("tracks") or [] if t.get("id")]
    for chunk in _chunks(track_ids, 50):
        created.add(list(chunk))
    result = {
        "action": "restore",
        "from": str(backup_path),
        "new_playlist_id": created.id,
        "name": playlist["name"],
        "tracks_added": len(track_ids),
    }
    log_action(result)
    return result


def delete_batch(
    session: tidalapi.Session,
    playlists: Sequence[Dict[str, Any]],
    reason: str = "triage",
    dry_run: bool = False,
    workers: int = 4,
    verbose: bool = True,
    check_stale: bool = True,
) -> Dict[str, Any]:
    """Delete playlists outright, with the same guards as a conversion.

    Used by triage, where there is no album to save — so the backup in data/backups
    is the only copy, and it is written before anything is deleted.
    """
    summary: Dict[str, Any] = {
        "requested": len(playlists),
        "deleted": 0,
        "delete_failed": [],
        "skipped_stale": [],
        "skipped_vanished": [],
        "backup_dir": None,
        "deleted_ids": set(),
        "dry_run": dry_run,
    }
    usable = list(playlists)
    if not usable:
        return summary

    if check_stale and not dry_run:
        changed, vanished = stale_playlists(session, usable, verbose=verbose)
        if changed or vanished:
            summary["skipped_stale"] = sorted(changed)
            summary["skipped_vanished"] = sorted(vanished)
            usable = [
                p for p in usable if p["id"] not in changed and p["id"] not in vanished
            ]

    if dry_run or not usable:
        summary["deleted"] = len(usable)
        return summary

    directory, _ = backup_batch(usable)
    summary["backup_dir"] = str(directory)
    if verbose:
        print(f"Backed up {len(usable)} playlist(s) to {directory}", file=sys.stderr)

    results = fetch.parallel_map(
        session,
        usable,
        lambda s, p: delete_playlist(s, p["id"]),
        label="deleting",
        workers=workers,
        verbose=verbose,
        note=lambda p: p["name"],
    )
    for index, playlist in enumerate(usable):
        value = results.get(index)
        ok = value is True
        if ok:
            summary["deleted"] += 1
            summary["deleted_ids"].add(playlist["id"])
        else:
            summary["delete_failed"].append(
                {
                    "playlist_id": playlist["id"],
                    "name": playlist["name"],
                    "error": value.get("__error__") if isinstance(value, dict) else str(value),
                }
            )
        log_action(
            {
                "action": "delete",
                "reason": reason,
                "playlist_id": playlist["id"],
                "playlist_name": playlist["name"],
                "num_tracks": len(playlist.get("tracks") or []),
                "playlist_deleted": ok,
                "backup_dir": str(directory),
            }
        )
    return summary
