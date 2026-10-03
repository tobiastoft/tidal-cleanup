"""The plan file: one tab-separated row per playlist, as the audit trail.

Written by `audit`, read by nothing but your eyes (and grep, and Numbers). `apply`
works off tiers rather than this file, so an edit here can never cause a deletion.
"""

from __future__ import annotations

import csv
from typing import Any, Dict, List

from .classify import TIER_ORDER
from .paths import PLAN_FILE, ensure_dirs

COLUMNS = [
    "tier",
    "verdict",
    "concentration",
    "coverage",
    "name_match",
    "tracks",
    "album_tracks",
    "extra_tracks",
    "album_audio",
    "atmos_tracks",
    "playlist",
    "matched_album",
    "album_artist",
    "playlist_id",
    "album_id",
    "playlist_url",
]


def write(entries: List[Dict[str, Any]]) -> int:
    ensure_dirs()
    rank = {t: i for i, t in enumerate(TIER_ORDER)}
    ordered = sorted(
        entries,
        key=lambda e: (
            rank.get(e.get("tier", "D"), 9),
            -e.get("coverage", 0.0),
            e["name"].lower(),
        ),
    )
    with PLAN_FILE.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, delimiter="\t")
        writer.writeheader()
        for entry in ordered:
            album = entry.get("album") or {}
            writer.writerow(
                {
                    "tier": entry.get("tier", "D"),
                    "verdict": entry["verdict"],
                    "concentration": f"{entry['concentration']:.2f}",
                    "coverage": f"{entry['coverage']:.2f}",
                    "name_match": f"{entry['name_score']:.2f}",
                    "tracks": entry["num_tracks"],
                    "album_tracks": album.get("num_tracks") or "",
                    "extra_tracks": entry.get("extra_tracks", ""),
                    "album_audio": "/".join(album.get("audio_modes") or []),
                    "atmos_tracks": entry.get("atmos_tracks") or "",
                    "playlist": entry["name"],
                    "matched_album": album.get("name") or "",
                    "album_artist": album.get("artist") or "",
                    "playlist_id": entry["playlist_id"],
                    "album_id": album.get("id") or "",
                    "playlist_url": entry.get("share_url") or "",
                }
            )
    return len(ordered)
