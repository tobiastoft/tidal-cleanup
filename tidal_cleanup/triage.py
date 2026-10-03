"""Fast keyboard triage of playlists: keep or delete, one keypress each.

Nothing is deleted while you are deciding. Decisions are written to disk as you
go, so quitting and resuming costs nothing, and the deletions happen in one
reviewed batch at the end.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from collections import Counter
from typing import Any, Dict, List, Optional

from . import actions, keys, report
from .paths import TRIAGE_FILE, ensure_dirs

ORDERS = ("name", "size", "date", "tier")
FULL_LIST_CAP = 60


def load_decisions() -> Dict[str, Any]:
    if TRIAGE_FILE.exists():
        try:
            return json.loads(TRIAGE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_decisions(decisions: Dict[str, Any]) -> None:
    ensure_dirs()
    tmp = TRIAGE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(decisions, indent=2, ensure_ascii=False))
    tmp.replace(TRIAGE_FILE)


def _duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "?"
    hours, rest = divmod(int(seconds), 3600)
    minutes = rest // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _when(stamp: Optional[str]) -> str:
    if not stamp:
        return "?"
    return str(stamp)[:10]


def order_queue(entries: List[Dict[str, Any]], order: str) -> List[Dict[str, Any]]:
    if order == "size":
        return sorted(entries, key=lambda e: -e["num_tracks"])
    if order == "date":
        return sorted(entries, key=lambda e: e.get("created") or "", reverse=True)
    if order == "tier":
        rank = {t: i for i, t in enumerate("DFCEBA")}
        return sorted(
            entries, key=lambda e: (rank.get(e.get("tier", "D"), 9), e["name"].lower())
        )
    return sorted(entries, key=lambda e: e["name"].lower())


def _sample_tracks(tracks: List[Dict[str, Any]], count: int = 6) -> List[Any]:
    """Tracks spread across the playlist, not just the first few.

    Imported playlists often repeat the same track near the top, so the opening
    rows say very little about what a 1900-track playlist actually holds.
    """
    if len(tracks) <= count:
        return list(enumerate(tracks, start=1))
    step = len(tracks) / count
    picked = []
    seen = set()
    for i in range(count):
        index = min(int(i * step), len(tracks) - 1)
        if index in seen:
            continue
        seen.add(index)
        picked.append((index + 1, tracks[index]))
    return picked


def _top_artists(tracks: List[Dict[str, Any]], count: int = 5) -> str:
    counts = Counter(t.get("artist") for t in tracks if t.get("artist"))
    if not counts:
        return ""
    return ", ".join(f"{name} ({n})" for name, n in counts.most_common(count))


def _render(
    entry: Dict[str, Any],
    playlist: Dict[str, Any],
    position: str,
    tally: str,
    show_all: bool,
    siblings: int = 0,
) -> None:
    tracks = playlist.get("tracks") or []
    print(report.paint("\n" + "─" * 74, "dim"))
    print(f"{report.paint(position, 'dim')}  {tally}")
    print(
        report.paint(entry["name"], "bold")
        + report.paint(f"   tier {entry.get('tier', '?')}", "dim")
    )

    distinct = len({t.get("artist") for t in tracks if t.get("artist")})
    duplicates = len(tracks) - len({(t.get("artist"), t.get("name")) for t in tracks})
    meta = (
        f"{entry['num_tracks']} tracks · {_duration(playlist.get('duration'))}"
        f" · {distinct} artists · created {_when(playlist.get('created'))}"
    )
    if duplicates:
        meta += f" · {duplicates} duplicate track(s)"
    if entry.get("atmos_tracks"):
        meta += f" · {entry['atmos_tracks']} Dolby Atmos"
    print(report.paint(meta, "dim"))
    if siblings:
        print(
            report.paint(
                f"⚠ {siblings} other playlist(s) have this same name", "yellow"
            )
        )
    if playlist.get("description"):
        print(report.paint(f"note: {playlist['description'][:100]}", "dim"))

    album = entry.get("album")
    if album and entry.get("precision", 0) >= 0.999:
        print(
            report.paint(
                f"↳ this is the album {album.get('artist')} / {album.get('name')} "
                f"({entry['coverage']:.0%} of it)"
                + (
                    "  [Dolby Atmos"
                    + ("-only" if album.get("atmos_only") else "")
                    + "]"
                    if album.get("atmos")
                    else ""
                ),
                "green",
            )
        )

    if len(tracks) > 12:
        artists = _top_artists(tracks)
        if artists:
            print(report.paint(f"most of it: {artists}", "cyan"))

    if show_all:
        rows = list(enumerate(tracks[:FULL_LIST_CAP], start=1))
    else:
        rows = _sample_tracks(tracks)
    for number, track in rows:
        print(
            f"  {number:>4}. {report.paint((track.get('artist') or '?')[:26], 'dim')}"
            f"  {track.get('name') or '?'}"
        )
    shown = len(rows)
    if show_all and len(tracks) > FULL_LIST_CAP:
        print(report.paint(f"       … {len(tracks) - FULL_LIST_CAP} more not shown", "dim"))
    elif not show_all and len(tracks) > shown:
        print(
            report.paint(
                f"       (sampled across {len(tracks)} tracks — [t] to list them)", "dim"
            )
        )


def _open_in_tidal(url: Optional[str]) -> None:
    if not url:
        print(report.paint("  no link for this playlist", "yellow"))
        return
    try:
        subprocess.run(["open", url], check=False, capture_output=True)
    except Exception as exc:
        print(report.paint(f"  could not open: {exc}", "yellow"))


def run(
    session,
    entries: List[Dict[str, Any]],
    playlists: Dict[str, Dict[str, Any]],
    order: str = "name",
    redo: bool = False,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """The triage loop. Returns the decisions; deleting is the caller's job."""
    decisions = {} if redo else load_decisions()
    queue = order_queue(
        [e for e in entries if redo or e["playlist_id"] not in decisions], order
    )
    if limit:
        queue = queue[:limit]

    if not queue:
        print(
            "Every playlist has already been triaged. "
            "Use `--redo` to start over, or `triage --commit` to apply."
        )
        return decisions

    if not keys.interactive():
        print(report.paint("(not a terminal: keys need Enter)", "dim"))

    name_counts = Counter(e["name"] for e in entries)
    history: List[str] = []
    show_all = False
    index = 0

    while index < len(queue):
        entry = queue[index]
        playlist = playlists[entry["playlist_id"]]
        marked = sum(1 for d in decisions.values() if d["decision"] == "delete")
        kept = sum(1 for d in decisions.values() if d["decision"] == "keep")
        tally = report.paint(
            f"keep {kept} · delete {marked} · {len(queue) - index} left", "dim"
        )
        _render(
            entry,
            playlist,
            f"[{index + 1}/{len(queue)}]",
            tally,
            show_all,
            siblings=name_counts[entry["name"]] - 1,
        )

        album = entry.get("album")
        convertible = bool(album and entry.get("precision", 0) >= 0.999)
        options = "  [d]elete  [k]eep  [s]kip  [u]ndo"
        if convertible:
            options += "  [a] save album + delete"
        options += "  [t]racks  [o]pen  [q]uit"
        print("\n" + options)

        try:
            choice = keys.read_key()
        except KeyboardInterrupt:
            print("\ninterrupted")
            break

        if choice == "t":
            show_all = not show_all
            continue
        if choice == "o":
            _open_in_tidal(playlist.get("share_url"))
            continue
        if choice == "q":
            break
        if choice == "u":
            if history:
                undone = history.pop()
                decisions.pop(undone, None)
                save_decisions(decisions)
                index = max(0, index - 1)
                print(report.paint("  undone", "dim"))
            else:
                print(report.paint("  nothing to undo", "dim"))
            show_all = False
            continue

        if choice == "d":
            decisions[entry["playlist_id"]] = {
                "decision": "delete",
                "name": entry["name"],
                "num_tracks": entry["num_tracks"],
                "at": datetime.now(timezone.utc).isoformat(),
            }
            print(report.paint("  marked for deletion", "red"))
        elif choice == "k":
            decisions[entry["playlist_id"]] = {
                "decision": "keep",
                "name": entry["name"],
                "at": datetime.now(timezone.utc).isoformat(),
            }
            # Keeping a playlist also means album conversion must not delete it.
            actions.add_ignored(entry["playlist_id"], entry["name"])
            print(report.paint("  keeping", "green"))
        elif choice == "a" and convertible:
            decisions[entry["playlist_id"]] = {
                "decision": "convert",
                "name": entry["name"],
                "album_id": album["id"],
                "album_name": album.get("name"),
                "at": datetime.now(timezone.utc).isoformat(),
            }
            print(report.paint("  marked: save album, then delete", "green"))
        elif choice in ("s", "\n", " ", ""):
            index += 1
            show_all = False
            continue
        else:
            print(report.paint("  unrecognised key", "yellow"))
            continue

        history.append(entry["playlist_id"])
        save_decisions(decisions)
        index += 1
        show_all = False

    return decisions


def pending(decisions: Dict[str, Any]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {"delete": [], "keep": [], "convert": []}
    for playlist_id, record in decisions.items():
        out.setdefault(record["decision"], []).append(playlist_id)
    return out
