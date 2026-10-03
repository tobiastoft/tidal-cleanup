"""Terminal output for the audit."""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

from .classify import TIER_LABELS, TIER_ORDER

_NO_COLOR = bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()

C = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "cyan": "\033[36m",
}

VERDICT_STYLE = {
    "album": ("green", "ALBUM"),
    "likely_album": ("green", "LIKELY ALBUM"),
    "album_plus_extras": ("yellow", "ALBUM + EXTRAS"),
    "album_subset": ("yellow", "PARTIAL ALBUM"),
    "curated": ("cyan", "CURATED"),
    "too_small": ("dim", "TOO SMALL"),
    "empty": ("dim", "EMPTY"),
    "unresolved": ("red", "UNRESOLVED"),
}

TIER_COLOR = {"A": "green", "B": "yellow", "C": "yellow", "D": "cyan",
               "E": "red", "F": "dim"}


def paint(text: str, color: str) -> str:
    if _NO_COLOR or color not in C:
        return text
    return f"{C[color]}{text}{C['reset']}"


def label(verdict: str) -> str:
    color, text = VERDICT_STYLE.get(verdict, ("dim", verdict.upper()))
    return paint(f"{text:<14}", color)


def line(entry: Dict[str, Any]) -> str:
    album = entry.get("album")
    count = paint(f"({entry['num_tracks']} tracks)", "dim")
    stats = paint(
        f"[conc {entry['concentration']:.2f} cov {entry['coverage']:.2f} "
        f"name {entry['name_score']:.2f}]",
        "dim",
    )
    atmos = ""
    if album and album.get("atmos"):
        atmos = paint(" ATMOS" + ("-only" if album.get("atmos_only") else ""), "cyan")
    elif entry.get("atmos_tracks"):
        atmos = paint(f" {entry['atmos_tracks']} atmos trk", "cyan")
    target = ""
    if album:
        artist = album.get("artist") or "?"
        target = paint(f"  ->  {artist} / {album['name']}", "dim") + atmos
    elif entry.get("album_guess"):
        guess = entry["album_guess"]
        target = paint(f"  ?->  {guess.get('artist') or '?'} / {guess.get('name')}", "dim")
    if not album:
        target += atmos
    return f"{label(entry['verdict'])} {entry['name']} {count} {stats}{target}"


def tier_counts(entries: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {t: 0 for t in TIER_ORDER}
    for entry in entries:
        counts[entry.get("tier", "D")] = counts.get(entry.get("tier", "D"), 0) + 1
    return counts


def summary(entries: List[Dict[str, Any]], sample: int = 0) -> str:
    """Tier counts, with a sample of each tier so the numbers aren't taken on faith."""
    buckets: Dict[str, List[Dict[str, Any]]] = {t: [] for t in TIER_ORDER}
    for entry in entries:
        buckets.setdefault(entry.get("tier", "D"), []).append(entry)

    out: List[str] = [paint(f"\n{len(entries)} playlists audited\n", "bold")]
    for tier in TIER_ORDER:
        group = sorted(buckets.get(tier) or [], key=lambda e: e["name"].lower())
        color = TIER_COLOR.get(tier, "dim")
        out.append(
            paint(f"TIER {tier}", color)
            + paint(f"  {len(group):>5}", "bold")
            + paint(f"   {TIER_LABELS[tier]}", "dim")
        )
        for entry in group[:sample]:
            out.append("        " + line(entry).strip())
        if sample and len(group) > sample:
            out.append(paint(f"        … {len(group) - sample} more", "dim"))
    out.append("")
    return "\n".join(out)
