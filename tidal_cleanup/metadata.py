"""Bulk edits to playlist metadata: notes (descriptions) and visibility.

Both go straight to the endpoints rather than through tidalapi:
  * `UserPlaylist.edit()` does `if not description: description = self.description`,
    so passing "" to clear a note silently keeps the old one;
  * `set_playlist_public/private` live on the v2 host and each triggers extra GETs
    to re-parse the playlist, which is wasted work across hundreds of playlists.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import tidalapi

from . import actions, fetch
from .paths import NOTES_BACKUP_DIR, ensure_dirs


# ------------------------------------------------------------------ writes

def _is_missing(error: Optional[str]) -> bool:
    """Did a write fail because the playlist is gone rather than something else?"""
    text = (error or "").lower()
    return "not found" in text or "404" in text


def alive(
    session: tidalapi.Session,
    playlists: Sequence[Dict[str, Any]],
    verbose: bool = True,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split a selection into playlists that still exist and ones that are gone.

    A snapshot goes stale as soon as a playlist is deleted elsewhere — in the Tidal
    app, or by another tool — and writing to one of those returns "Object not
    found". Checking first costs a couple of requests and turns a confusing failure
    into a plain statement.
    """
    if verbose:
        print("Checking which playlists still exist...", file=sys.stderr)
    live = {p["id"] for p in fetch.list_owned_playlists(session, verbose=False)}
    here = [p for p in playlists if p["id"] in live]
    gone = [p for p in playlists if p["id"] not in live]
    return here, gone


def set_note(
    session: tidalapi.Session, playlist_id: str, title: str, description: str
) -> bool:
    """Replace a playlist's description. An empty string genuinely clears it."""
    response = session.request.request(
        "POST", f"playlists/{playlist_id}", data={"title": title, "description": description}
    )
    return response.ok


def set_visibility(session: tidalapi.Session, playlist_id: str, public: bool) -> bool:
    """Flip a playlist public or private (v2 endpoint)."""
    verb = "set-public" if public else "set-private"
    response = session.request.request(
        "PUT",
        path=f"playlists/{playlist_id}/{verb}",
        base_url=session.config.api_v2_location,
    )
    return response.ok


# ------------------------------------------------------------------ notes

def note_groups(playlists: Sequence[Dict[str, Any]]) -> List[Any]:
    """Distinct notes with how many playlists carry each, commonest first."""
    counts = Counter(
        (p.get("description") or "").strip()
        for p in playlists
        if (p.get("description") or "").strip()
    )
    return counts.most_common()


def select_by_note(
    playlists: Sequence[Dict[str, Any]], match: Optional[str], every: bool
) -> List[Dict[str, Any]]:
    """Playlists whose note contains `match` (case-insensitive), or all noted ones."""
    chosen = []
    needle = (match or "").lower()
    for playlist in playlists:
        note = (playlist.get("description") or "").strip()
        if not note:
            continue
        if every or (needle and needle in note.lower()):
            chosen.append(playlist)
    return chosen


def backup_notes(playlists: Sequence[Dict[str, Any]]) -> Path:
    """Save the notes about to be cleared, so `notes --restore` can put them back."""
    ensure_dirs()
    NOTES_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = NOTES_BACKUP_DIR / f"{stamp}-notes.json"
    path.write_text(
        json.dumps(
            {
                "saved_at": stamp,
                "notes": [
                    {
                        "playlist_id": p["id"],
                        "name": p["name"],
                        "description": p.get("description") or "",
                    }
                    for p in playlists
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return path


def clear_notes(
    session: tidalapi.Session,
    playlists: Sequence[Dict[str, Any]],
    replacement: str = "",
    dry_run: bool = False,
    workers: int = 4,
    verbose: bool = True,
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "requested": len(playlists),
        "changed": 0,
        "failed": [],
        "backup": None,
        "dry_run": dry_run,
    }
    if not playlists or dry_run:
        summary["changed"] = len(playlists)
        return summary

    summary["backup"] = str(backup_notes(playlists))
    if verbose:
        print(f"Notes backed up to {summary['backup']}", file=sys.stderr)

    results = fetch.parallel_map(
        session,
        list(playlists),
        lambda s, p: set_note(s, p["id"], p["name"], replacement),
        label="notes",
        workers=workers,
        verbose=verbose,
        note=lambda p: p["name"],
    )
    for index, playlist in enumerate(playlists):
        value = results.get(index)
        ok = value is True
        if ok:
            summary["changed"] += 1
        else:
            error = value.get("__error__") if isinstance(value, dict) else str(value)
            summary["failed"].append(
                {
                    "playlist_id": playlist["id"],
                    "name": playlist["name"],
                    "error": error,
                    "vanished": _is_missing(error),
                }
            )
        actions.log_action(
            {
                "action": "clear_note" if not replacement else "set_note",
                "playlist_id": playlist["id"],
                "playlist_name": playlist["name"],
                "previous": (playlist.get("description") or "")[:500],
                "new": replacement,
                "ok": ok,
                "backup": summary["backup"],
            }
        )
    return summary


def list_note_backups() -> List[Path]:
    if not NOTES_BACKUP_DIR.exists():
        return []
    return sorted(NOTES_BACKUP_DIR.glob("*.json"))


def restore_notes(
    session: tidalapi.Session, path: Path, workers: int = 4, verbose: bool = True
) -> Dict[str, Any]:
    """Put back the notes recorded in a backup file."""
    data = json.loads(Path(path).read_text())
    rows = data.get("notes") or []
    results = fetch.parallel_map(
        session,
        rows,
        lambda s, r: set_note(s, r["playlist_id"], r["name"], r["description"]),
        label="restoring",
        workers=workers,
        verbose=verbose,
        note=lambda r: r["name"],
    )
    restored = sum(1 for v in results.values() if v is True)
    actions.log_action(
        {"action": "restore_notes", "from": str(path), "restored": restored,
         "of": len(rows)}
    )
    return {"restored": restored, "of": len(rows)}


# ------------------------------------------------------------------ visibility

def apply_visibility(
    session: tidalapi.Session,
    playlists: Sequence[Dict[str, Any]],
    public: bool,
    dry_run: bool = False,
    workers: int = 4,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Flip a set of playlists public or private, skipping ones already there."""
    todo = [p for p in playlists if bool(p.get("public")) is not public]
    summary: Dict[str, Any] = {
        "requested": len(playlists),
        "already": len(playlists) - len(todo),
        "changed": 0,
        "failed": [],
        "public": public,
        "dry_run": dry_run,
    }
    if not todo or dry_run:
        summary["changed"] = len(todo)
        return summary

    results = fetch.parallel_map(
        session,
        todo,
        lambda s, p: set_visibility(s, p["id"], public),
        label="public" if public else "private",
        workers=workers,
        verbose=verbose,
        note=lambda p: p["name"],
    )
    for index, playlist in enumerate(todo):
        value = results.get(index)
        ok = value is True
        if ok:
            summary["changed"] += 1
        else:
            error = value.get("__error__") if isinstance(value, dict) else str(value)
            summary["failed"].append(
                {
                    "playlist_id": playlist["id"],
                    "name": playlist["name"],
                    "error": error,
                    "vanished": _is_missing(error),
                }
            )
        actions.log_action(
            {
                "action": "set_public" if public else "set_private",
                "playlist_id": playlist["id"],
                "playlist_name": playlist["name"],
                "was_public": bool(playlist.get("public")),
                "ok": ok,
            }
        )
    return summary
