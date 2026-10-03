"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import random
import sys
from itertools import zip_longest
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import (
    __version__, actions, atmos, auth, fetch, keys, metadata, plan, report,
    snapshot, triage,
)
from .classify import TIER_LABELS, TIER_ORDER, annotate, classify_all, normalize
from .paths import ACTION_LOG, PLAN_FILE, SNAPSHOT_FILE


# ---------------------------------------------------------------- helpers

def _entries(session, refresh: bool, workers: int) -> tuple:
    snap = snapshot.load_or_build(session, refresh=refresh, workers=workers)
    entries = annotate(classify_all(snap))
    return snap, entries, {p["id"]: p for p in snap.get("playlists") or []}


def _select(
    entries: List[Dict[str, Any]],
    tiers: str,
    ignored: set,
    limit: Optional[int] = None,
    shuffle: bool = False,
) -> List[Dict[str, Any]]:
    wanted = set(tiers.upper())
    picked = [
        e
        for e in entries
        if e.get("tier") in wanted and e["playlist_id"] not in ignored
    ]
    if shuffle:
        random.shuffle(picked)
    else:
        rank = {t: i for i, t in enumerate(TIER_ORDER)}
        picked.sort(key=lambda e: (rank.get(e.get("tier", "D"), 9), e["name"].lower()))
    return picked[:limit] if limit else picked


def _confirm(prompt: str, expect: str = "yes") -> bool:
    try:
        answer = input(f"{prompt} (type '{expect}' to proceed) ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer == expect


def _show_details(session, playlist: Dict[str, Any], entry: Dict[str, Any]) -> None:
    album = entry.get("album") or {}
    print(report.paint("\n  Playlist tracks:", "bold"))
    for index, track in enumerate(playlist.get("tracks") or [], start=1):
        marker = " " if track.get("album_id") == album.get("id") else "*"
        print(
            f"   {marker}{index:>3}. {track['name']}"
            + report.paint("  — " + (track.get("album_name") or "?"), "dim")
        )
    if not album.get("id"):
        return
    try:
        payload = fetch.get_json(session, f"albums/{album['id']}/items", params={"limit": 100})
        album_titles = [
            (i.get("item", i) or {}).get("title") for i in (payload.get("items") or [])
        ]
    except Exception as exc:
        print(report.paint(f"  (could not load album tracks: {exc})", "dim"))
        return

    have = {normalize(t["name"]) for t in playlist.get("tracks") or []}
    missing = [t for t in album_titles if t and normalize(t) not in have]
    print(
        report.paint(
            f"\n  Album '{album['name']}' has {len(album_titles)} tracks.", "bold"
        )
    )
    if missing:
        print(report.paint("  Not in the playlist: " + ", ".join(missing), "yellow"))
    else:
        print(report.paint("  Every album track is in the playlist.", "green"))
    extra = [
        t["name"]
        for t in playlist.get("tracks") or []
        if t.get("album_id") != album.get("id")
    ]
    if extra:
        print(report.paint("  * Not from this album: " + ", ".join(extra), "yellow"))
    print()


# ---------------------------------------------------------------- commands

def cmd_login(args: argparse.Namespace) -> int:
    session = auth.login()
    print(f"Logged in as {session.user.username or session.user.id}")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    auth.logout()
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    session = auth.login()
    snap, entries, _ = _entries(session, args.refresh, args.workers)

    if args.json:
        print(json.dumps(entries, indent=2, ensure_ascii=False))
        return 0

    print(report.summary(entries, sample=args.sample))
    count = plan.write(entries)
    counts = report.tier_counts(entries)
    print(report.paint(f"Plan written to {PLAN_FILE} ({count} rows)", "dim"))
    print(report.paint(f"Snapshot: {SNAPSHOT_FILE}", "dim"))

    ignored = actions.load_ignored()
    if ignored:
        print(report.paint(f"{len(ignored)} playlist(s) marked never-ask", "dim"))

    print()
    if counts.get("A"):
        print("Next steps:")
        print(f"  ./tidalcleanup review --tier A --sample 20   # spot-check tier A")
        print(f"  ./tidalcleanup apply --tier A --dry-run      # rehearse")
        print(f"  ./tidalcleanup apply --tier A                # convert {counts['A']}")
    if counts.get("B"):
        print(f"  ./tidalcleanup review --tier B               # {counts['B']} renamed")
    if counts.get("C"):
        print(f"  ./tidalcleanup review --tier C               # {counts['C']} by hand")
    if counts.get("E"):
        print(
            f"  {counts['E']} playlist(s) in tier E: album-shaped, but the album no "
            f"longer resolves on Tidal."
        )
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    session = auth.login()
    snap, entries, playlists = _entries(session, args.refresh, args.workers)
    ignored = actions.load_ignored()

    asked = set(args.tier.upper())
    if not asked <= {"A", "B", "C"}:
        refused = sorted(asked - {"A", "B", "C"})
        reasons = {
            "D": "tier D is your curated playlists",
            "E": "tier E albums don't resolve on Tidal yet — run "
                 "`audit --refresh` to try re-finding them by search",
            "F": "tier F playlists are empty or single-track, with no album to save",
        }
        for tier in refused:
            print(
                f"Refusing to apply to tier {tier} — "
                f"{reasons.get(tier, 'not a convertible tier')}.",
                file=sys.stderr,
            )
        return 2

    queue = _select(entries, args.tier, ignored, limit=args.limit)
    if not queue:
        print(f"Nothing in tier(s) {args.tier.upper()} to apply.")
        return 0

    counts: Dict[str, int] = {}
    for entry in queue:
        counts[entry["tier"]] = counts.get(entry["tier"], 0) + 1
    breakdown = ", ".join(f"{n} in tier {t}" for t, n in sorted(counts.items()))

    print(report.paint(f"\n{len(queue)} playlist(s) selected ({breakdown}).", "bold"))
    for entry in queue[:10]:
        print("   " + report.line(entry).strip())
    if len(queue) > 10:
        print(report.paint(f"   … {len(queue) - 10} more (see {PLAN_FILE})", "dim"))

    action = "save albums" if args.keep_playlists else "save albums and DELETE playlists"
    print(f"\nAbout to {action} for {len(queue)} playlist(s).")

    if args.dry_run:
        print(report.paint("DRY RUN — nothing will be changed.\n", "yellow"))
    elif not args.yes:
        if not _confirm("Proceed?"):
            print("Aborted.")
            return 1

    collection = actions.Collection(session)
    result = actions.apply_batch(
        session,
        collection,
        queue,
        playlists,
        add_artist=not args.no_artists,
        delete=not args.keep_playlists,
        dry_run=args.dry_run,
        workers=args.workers,
        check_stale=not args.skip_stale_check,
    )

    print()
    if args.dry_run:
        print(
            f"Would save {result['albums_saved']} new album(s), "
            f"follow {result['artists_saved']} artist(s), "
            f"delete {result['deleted']} playlist(s)."
        )
        return 0

    print(report.paint(f"{result['albums_saved']} new album(s) saved.", "green"))
    if result["artists_saved"]:
        print(report.paint(f"{result['artists_saved']} artist(s) in collection.", "green"))
    print(report.paint(f"{result['deleted']} playlist(s) deleted.", "green"))
    if result["backup_dir"]:
        print(report.paint(f"Backups: {result['backup_dir']}", "dim"))

    for key, message in (
        ("skipped_stale", "changed since the audit, left alone"),
        ("skipped_vanished", "no longer exist, left alone"),
        ("albums_unconfirmed", "album(s) could not be saved; their playlists were kept"),
    ):
        if result.get(key):
            print(report.paint(f"{len(result[key])} {message}.", "yellow"))
    if result.get("skipped_stale"):
        print(
            report.paint(
                "  Re-run `audit --refresh` to pick up the changes, then apply again.",
                "dim",
            )
        )
    if result.get("delete_failed"):
        print(report.paint(f"{len(result['delete_failed'])} delete(s) failed:", "red"))
        for failure in result["delete_failed"][:10]:
            print(report.paint(f"  {failure['name']}: {failure['error']}", "red"))

    if result.get("deleted_ids"):
        snapshot.drop(set(result["deleted_ids"]))
        remaining = annotate(classify_all(snapshot.load()))
        plan.write(remaining)
        print(report.paint("Snapshot and plan updated.", "dim"))
    print(report.paint(f"Log: {ACTION_LOG}", "dim"))
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    session = auth.login()
    snap, entries, playlists = _entries(session, args.refresh, args.workers)
    ignored = actions.load_ignored()
    queue = _select(
        entries,
        args.tier,
        ignored,
        limit=args.sample or args.limit,
        shuffle=bool(args.sample),
    )

    if not queue:
        print(f"Nothing in tier(s) {args.tier.upper()} to review.")
        return 0
    if args.sample:
        print(
            report.paint(
                f"Spot-checking {len(queue)} random playlist(s) from tier "
                f"{args.tier.upper()}.\n",
                "bold",
            )
        )
    if args.dry_run:
        print(report.paint("DRY RUN — nothing will be saved or deleted.\n", "yellow"))

    collection = actions.Collection(session)
    converted = saved_only = skipped = 0
    index = 0

    while index < len(queue):
        entry = queue[index]
        playlist = playlists[entry["playlist_id"]]

        print(report.paint(f"\n{'─' * 72}", "dim"))
        print(
            f"[{index + 1}/{len(queue)}] tier {entry['tier']}  "
            + report.paint(entry["name"], "bold")
        )
        print(f"   {report.line(entry)}")
        for reason in entry["reasons"]:
            print(report.paint(f"   · {reason}", "dim"))
        if entry["description"]:
            print(report.paint(f"   note: {entry['description'][:120]}", "dim"))
        print(
            "\n   [y] save album + delete playlist   [a] save album, keep playlist\n"
            "   [s] skip        [n] never ask again   [d] details   [q] quit"
        )
        try:
            choice = input("   > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            choice = "q"

        if choice == "d":
            _show_details(session, playlist, entry)
            continue
        if choice == "q":
            break
        if choice == "n":
            actions.add_ignored(entry["playlist_id"], entry["name"])
            print(report.paint("   marked never-ask", "dim"))
            index += 1
            continue
        if choice in ("", "s"):
            skipped += 1
            index += 1
            continue
        if choice not in ("y", "a"):
            print(report.paint("   unrecognised key", "yellow"))
            continue

        outcome = actions.convert_one(
            session,
            collection,
            playlist,
            entry,
            add_artist=not args.no_artists,
            delete=choice == "y" and not args.keep_playlists,
            dry_run=args.dry_run,
        )
        if outcome.get("error"):
            print(report.paint(f"   ! {outcome['error']}", "red"))
        else:
            bits = ["album saved" if outcome["album_saved"] else "album NOT saved"]
            if outcome["artist_saved"]:
                bits.append("artist saved")
            bits.append(
                "playlist deleted" if outcome["playlist_deleted"] else "playlist kept"
            )
            print(report.paint("   " + ", ".join(bits), "green"))
        if outcome.get("backup"):
            print(report.paint(f"   backup: {outcome['backup']}", "dim"))

        converted += 1 if outcome["playlist_deleted"] else 0
        saved_only += 0 if outcome["playlist_deleted"] else 1
        index += 1

    print(report.paint(f"\n{'─' * 72}", "dim"))
    print(
        f"{converted} converted and deleted, {saved_only} album(s) saved with the "
        f"playlist kept, {skipped} skipped."
    )
    if converted:
        print(
            report.paint(
                "Run `./tidalcleanup audit --refresh` to re-sync the snapshot.", "dim"
            )
        )
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    session = auth.login()
    snap, entries, playlists = _entries(session, args.refresh, args.workers)

    # Keep the snapshot's extra playlist fields available to the loop's display.
    for entry in entries:
        source = playlists.get(entry["playlist_id"]) or {}
        entry["created"] = source.get("created")

    if args.status:
        decisions = triage.load_decisions()
        groups = triage.pending(decisions)
        print(
            f"{len(decisions)} of {len(entries)} playlists triaged: "
            f"{len(groups['keep'])} keep, {len(groups['delete'])} to delete, "
            f"{len(groups['convert'])} to convert."
        )
        for playlist_id in groups["delete"][:20]:
            print(report.paint(f"  delete  {decisions[playlist_id]['name']}", "red"))
        if len(groups["delete"]) > 20:
            print(report.paint(f"  … {len(groups['delete']) - 20} more", "dim"))
        return 0

    selected = entries
    if args.tier:
        selected = [e for e in entries if e.get("tier") in set(args.tier.upper())]
        if not selected:
            print(f"No playlists in tier(s) {args.tier.upper()}.")
            return 0

    if not args.commit:
        triage.run(
            session,
            selected,
            playlists,
            order=args.order,
            redo=args.redo,
            limit=args.limit,
        )

    decisions = triage.load_decisions()
    groups = triage.pending(decisions)
    to_delete = [playlists[p] for p in groups["delete"] if p in playlists]
    to_convert = [
        e for e in entries if e["playlist_id"] in set(groups["convert"])
    ]

    print(report.paint(f"\n{'─' * 74}", "dim"))
    print(
        f"{len(groups['keep'])} kept · {len(to_delete)} marked for deletion · "
        f"{len(to_convert)} marked for album conversion"
    )
    if not to_delete and not to_convert:
        print("Nothing to apply.")
        return 0

    for playlist in to_delete[:15]:
        print(
            report.paint(
                f"  delete  {playlist['name']}  "
                f"({len(playlist.get('tracks') or [])} tracks)",
                "red",
            )
        )
    if len(to_delete) > 15:
        print(report.paint(f"  … {len(to_delete) - 15} more", "dim"))

    if args.dry_run:
        print(report.paint("\nDRY RUN — nothing deleted.", "yellow"))
        return 0
    if not args.yes and not _confirm(
        f"\nPermanently delete {len(to_delete)} playlist(s)?"
    ):
        print("Nothing applied. Your decisions are saved; re-run with --commit.")
        return 1

    deleted_ids = set()
    if to_convert:
        collection = actions.Collection(session)
        result = actions.apply_batch(
            session, collection, to_convert, playlists, workers=args.workers
        )
        print(
            report.paint(
                f"{result['albums_saved']} album(s) saved, "
                f"{result['deleted']} playlist(s) converted.",
                "green",
            )
        )
        deleted_ids |= set(result.get("deleted_ids") or set())

    if to_delete:
        result = actions.delete_batch(
            session, to_delete, workers=args.workers, check_stale=not args.skip_stale_check
        )
        print(report.paint(f"{result['deleted']} playlist(s) deleted.", "green"))
        if result["backup_dir"]:
            print(report.paint(f"Backups: {result['backup_dir']}", "dim"))
        for key, message in (
            ("skipped_stale", "changed since the audit, left alone"),
            ("skipped_vanished", "already gone"),
        ):
            if result.get(key):
                print(report.paint(f"{len(result[key])} {message}.", "yellow"))
        if result["delete_failed"]:
            print(report.paint(f"{len(result['delete_failed'])} failed:", "red"))
            for failure in result["delete_failed"][:10]:
                print(report.paint(f"  {failure['name']}: {failure['error']}", "red"))
        deleted_ids |= set(result.get("deleted_ids") or set())

    if deleted_ids:
        snapshot.drop(deleted_ids)
        remaining = annotate(classify_all(snapshot.load()))
        plan.write(remaining)
        decisions = {
            pid: rec for pid, rec in triage.load_decisions().items()
            if pid not in deleted_ids
        }
        triage.save_decisions(decisions)
        print(report.paint("Snapshot, plan and decisions updated.", "dim"))
    print(report.paint(f"Log: {ACTION_LOG}", "dim"))
    return 0


def _review_atmos(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Step through the candidates that need a decision, one keypress each.

    Nothing is applied here; the accepted list goes back to the caller, which
    confirms and applies it as a batch.
    """
    accepted: List[Dict[str, Any]] = []
    show_tracks = False
    index = 0

    if not keys.interactive():
        print(report.paint("(not a terminal: keys need Enter)", "dim"))

    while index < len(candidates):
        record = candidates[index]
        stereo, atmos_album = record["stereo"], record["atmos"]
        lost = record.get("missing") or []

        print(report.paint("\n" + "─" * 74, "dim"))
        print(
            f"{report.paint(f'[{index + 1}/{len(candidates)}]', 'dim')}  "
            f"{report.paint(f'accepted {len(accepted)}', 'dim')}"
        )
        print(
            report.paint(str(stereo.get("artist") or "?"), "bold")
            + report.paint(f"   {record['match']} match", "dim")
        )
        print(f"  have:  {stereo.get('name')}  ({record['stereo_tracks']} tracks)")
        print(
            f"  Atmos: {atmos_album.get('title')}  ({record['atmos_tracks']} tracks)"
        )

        if lost:
            print(
                report.paint(
                    f"\n  you would LOSE {len(lost)} track(s):", "red"
                )
            )
            for title in lost:
                print(report.paint(f"    - {title}", "red"))
        else:
            print(report.paint("\n  every track you have is on the Atmos edition", "green"))
            if record.get("extra"):
                print(report.paint(f"  and it adds {record['extra']} more", "dim"))

        if show_tracks:
            mine = atmos.cached_tracklist(stereo["id"])
            theirs = atmos.cached_tracklist(atmos_album["id"])
            width = max((len(t) for t in mine), default=0) + 2
            print(report.paint("\n  yours".ljust(width + 4) + "Atmos", "dim"))
            for left, right in zip_longest(mine, theirs, fillvalue=""):
                print(f"    {left[:width]:<{width}}{right[:44]}")

        print(
            "\n  [y] accept this swap   [n] skip   [t] full tracklists   "
            "[o] open in Tidal   [q] stop"
        )
        try:
            choice = keys.read_key()
        except KeyboardInterrupt:
            print("\ninterrupted")
            break

        if choice == "t":
            show_tracks = not show_tracks
            continue
        if choice == "o":
            for url in (
                f"https://tidal.com/album/{atmos_album['id']}",
                stereo.get("share_url"),
            ):
                if url:
                    triage._open_in_tidal(url)
            continue
        if choice == "q":
            break
        if choice == "y":
            accepted.append(record)
            print(report.paint("   accepted", "green"))
        elif choice in ("", "n", "s", "\n", " "):
            print(report.paint("   skipped", "dim"))
        else:
            print(report.paint("   unrecognised key", "yellow"))
            continue
        show_tracks = False
        index += 1

    return accepted


def cmd_atmos(args: argparse.Namespace) -> int:
    session = auth.login()
    collection = actions.Collection(session)
    found = atmos.find_upgrades(
        session, collection.album_ids, workers=args.workers,
        loose=not args.titles_only,
    )
    upgrades = list(found["upgrades"])
    if args.include_retitled:
        upgrades += found.get("retitled") or []
    if args.max_loss:
        # Deliberately lossy: the user has said a small loss is acceptable.
        upgrades += [
            r
            for r in found.get("near_misses") or []
            if len(r["missing"]) <= args.max_loss
        ]
    if args.limit:
        upgrades = upgrades[: args.limit]

    print(report.paint(f"\n{len(collection.album_ids)} saved albums", "bold"))
    print(f"  {len(found['already_atmos'])} are already Dolby Atmos")
    print(
        f"  {found['stereo_total']} are stereo; {found['stereo_checked']} had a "
        f"same-titled Atmos edition to compare"
    )
    print(
        report.paint(
            f"  {len(found['upgrades'])} can be upgraded with no loss of tracks",
            "green",
        )
    )
    if found.get("retitled"):
        print(
            report.paint(
                f"  {len(found['retitled'])} more match by tracklist under a "
                f"different title (needs --include-retitled)",
                "cyan",
            )
        )
    near = found.get("near_misses") or []
    if near:
        print(
            report.paint(
                f"  {len(near)} same-titled Atmos edition(s) would lose tracks "
                f"(see below; --max-loss N to accept a small loss)",
                "yellow",
            )
        )
    if found.get("discarded"):
        print(
            report.paint(
                f"  {found['discarded']} candidate(s) compared and discarded "
                f"(a different album by the same artist)",
                "dim",
            )
        )

    for upgrade in upgrades[:25]:
        stereo = upgrade["stereo"]
        extra = f" (+{upgrade['extra']} extra)" if upgrade["extra"] else ""
        print(
            f"    {str(stereo.get('artist'))[:22]:<22} {str(stereo.get('name'))[:34]:<34} "
            f"{upgrade['stereo_tracks']:>3} -> {upgrade['atmos_tracks']:>3} tracks{extra}"
        )
    if len(upgrades) > 25:
        print(report.paint(f"    … {len(upgrades) - 25} more", "dim"))

    if found.get("retitled"):
        print(report.paint("\n  matched by tracklist, title differs:", "cyan"))
        for record in found["retitled"][:20]:
            print(
                f"    {str(record['stereo'].get('artist'))[:20]:<20} "
                f"{str(record['stereo'].get('name'))[:30]:<30} -> "
                f"{str(record['atmos'].get('title'))[:34]:<34} "
                f"{record['stereo_tracks']}/{record['atmos_tracks']} tracks"
            )
        if not args.include_retitled:
            print(
                report.paint(
                    "    (check these titles; pass --include-retitled to apply them)",
                    "dim",
                )
            )

    if near:
        print(
            report.paint(
                "\n  same album in Atmos, but tracks would be lost:", "yellow"
            )
        )
        for record in near:
            lost = len(record["missing"])
            flag = "green" if lost <= (args.max_loss or 0) else "yellow"
            print(
                report.paint(
                    f"    lose {lost}/{record['stereo_tracks']}  "
                    f"{str(record['stereo'].get('artist'))[:18]:<18} "
                    f"{str(record['stereo'].get('name'))[:30]:<30} -> "
                    f"{str(record['atmos'].get('title'))[:28]}",
                    flag,
                )
            )
            print(
                report.paint(
                    f"        would lose: {', '.join(record['missing'][:6])}"
                    + (" …" if lost > 6 else ""),
                    "dim",
                )
            )

    if args.show_rejected:
        print(report.paint("\n  rejected candidates:", "yellow"))
        # Group by the saved album, so one album with six bad candidates reads as
        # one line rather than six near-identical ones.
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for record in found["partial"]:
            grouped.setdefault(str(record["stereo"]["id"]), []).append(record)
        for records in list(grouped.values())[:20]:
            stereo = records[0]["stereo"]
            print(
                f"    {str(stereo.get('artist'))[:20]:<20} "
                f"{str(stereo.get('name'))[:32]:<32} "
                + report.paint(f"{len(records)} candidate(s) rejected", "dim")
            )
            for record in records[:2]:
                why = (
                    record.get("rejected")
                    or f"missing {len(record['missing'])}: "
                    f"{', '.join(record['missing'][:3])}"
                )
                print(
                    report.paint(
                        f"        vs {str(record['atmos'].get('title'))[:40]:<40} "
                        f"[{record['match']}] {why}",
                        "dim",
                    )
                )
        if len(grouped) > 20:
            print(report.paint(f"    … {len(grouped) - 20} more albums", "dim"))

    if args.review:
        candidates = atmos.review_candidates(found)
        if not candidates:
            print("\nNothing needs a decision.")
            return 0
        print(
            report.paint(
                f"\nStepping through {len(candidates)} candidate(s) that need a "
                f"decision.",
                "bold",
            )
        )
        upgrades = _review_atmos(candidates)
        if not upgrades:
            print("\nNothing accepted.")
            return 0
        args.apply = True

    if not args.apply:
        print(
            report.paint(
                "\nRun with --review to step through the ones needing a decision, "
                "or --apply to take the clean upgrades.",
                "dim",
            )
        )
        return 0
    if not upgrades:
        print("Nothing to upgrade.")
        return 0

    action = (
        "save the Atmos editions and remove the stereo ones"
        if not args.keep_stereo
        else "save the Atmos editions, keeping the stereo ones"
    )
    lossy = [u for u in upgrades if u.get("missing")]
    print(f"\nAbout to {action} for {len(upgrades)} album(s).")
    if lossy:
        total_lost = sum(len(u["missing"]) for u in lossy)
        print(
            report.paint(
                f"{len(lossy)} of them lose {total_lost} track(s) in total, "
                f"which the Atmos edition does not have:",
                "yellow",
            )
        )
        for upgrade in lossy:
            print(
                report.paint(
                    f"  {upgrade['stereo'].get('name')}: "
                    f"{', '.join(upgrade['missing'])}",
                    "yellow",
                )
            )
    if args.dry_run:
        print(report.paint("DRY RUN — nothing will change.", "yellow"))
    elif not args.yes and not _confirm("Proceed?"):
        print("Aborted.")
        return 1

    result = atmos.apply_upgrades(
        session,
        collection,
        upgrades,
        remove_stereo=not args.keep_stereo,
        dry_run=args.dry_run,
    )
    print()
    if args.dry_run:
        print(
            f"Would save {result['atmos_saved']} Atmos album(s) and remove "
            f"{result['stereo_removed']} stereo one(s)."
        )
        return 0
    print(report.paint(f"{result['atmos_saved']} Atmos album(s) saved.", "green"))
    print(report.paint(f"{result['stereo_removed']} stereo album(s) removed.", "green"))
    if result["unconfirmed"]:
        print(
            report.paint(
                f"{len(result['unconfirmed'])} Atmos album(s) did not save; their "
                f"stereo versions were kept.",
                "yellow",
            )
        )
    if result["remove_failed"]:
        print(report.paint(f"{len(result['remove_failed'])} removal(s) failed:", "red"))
        for failure in result["remove_failed"][:10]:
            print(report.paint(f"  {failure['name']}: {failure['error']}", "red"))
    print(report.paint(f"Every swap is logged in {ACTION_LOG}", "dim"))
    return 0


def _pick(entries, playlists, args, by_name: bool):
    """Resolve --tier / --match / --all into a list of playlist records."""
    chosen = entries
    if args.tier:
        chosen = [e for e in chosen if e.get("tier") in set(args.tier.upper())]
    rows = [playlists[e["playlist_id"]] for e in chosen if e["playlist_id"] in playlists]
    if by_name and args.match:
        needle = args.match.lower()
        rows = [p for p in rows if needle in (p.get("name") or "").lower()]
    return rows


def _report_failures(result: Dict[str, Any], label: str) -> set:
    """Print failures, separating vanished playlists from genuine errors.

    Returns the ids of playlists that no longer exist, so the caller can prune
    them from the snapshot.
    """
    failures = result.get("failed") or []
    if not failures:
        return set()
    vanished = [f for f in failures if f.get("vanished")]
    real = [f for f in failures if not f.get("vanished")]
    if vanished:
        print(
            report.paint(
                f"{len(vanished)} playlist(s) no longer exist and were skipped:",
                "dim",
            )
        )
        for failure in vanished[:10]:
            print(report.paint(f"  {failure['name']}", "dim"))
    if real:
        print(report.paint(f"{len(real)} {label} failed:", "red"))
        for failure in real[:10]:
            print(report.paint(f"  {failure['name']}: {failure['error']}", "red"))
    return {f["playlist_id"] for f in vanished}


def cmd_notes(args: argparse.Namespace) -> int:
    session = auth.login()

    if args.restore or args.list_backups:
        backups = metadata.list_note_backups()
        if args.list_backups or not args.restore:
            if not backups:
                print("No note backups yet.")
                return 0
            for path in backups:
                rows = json.loads(path.read_text()).get("notes") or []
                print(f"{path.name}  ({len(rows)} notes)")
            return 0
        target = Path(args.restore)
        if not target.exists():
            matches = [b for b in backups if args.restore in b.name]
            if len(matches) != 1:
                print(f"'{args.restore}' matched {len(matches)} backups.", file=sys.stderr)
                return 1
            target = matches[0]
        result = metadata.restore_notes(session, target, workers=args.workers)
        print(f"Restored {result['restored']} of {result['of']} note(s).")
        return 0

    snap, entries, playlists = _entries(session, args.refresh, args.workers)
    rows = _pick(entries, playlists, args, by_name=False)
    groups = metadata.note_groups(rows)

    noted = len([p for p in rows if (p.get("description") or "").strip()])
    print(report.paint(f"\n{len(rows)} playlists · {noted} carry a note", "bold"))
    if not groups:
        print("No notes to scrub.")
        return 0
    print(f"{len(groups)} distinct note(s):\n")
    for text, count in groups[: args.show]:
        print(report.paint(f"  {count:>4}x", "bold") + f"  {text[:120]!r}")
    if len(groups) > args.show:
        print(report.paint(f"  … {len(groups) - args.show} more", "dim"))

    if not args.clear:
        print(
            report.paint(
                "\nTo scrub: --clear --match tunemymusic   (or --clear --all)", "dim"
            )
        )
        return 0

    if not args.match and not args.all:
        print(
            "Refusing to clear without a selection. Pass --match TEXT or --all.",
            file=sys.stderr,
        )
        return 2

    selected = metadata.select_by_note(rows, args.match, args.all)
    if not selected:
        print("Nothing matched.")
        return 0

    print(report.paint(f"\n{len(selected)} playlist(s) would have the note cleared:", "bold"))
    for playlist in selected[:12]:
        print(f"  {playlist['name'][:50]:<50} {(playlist.get('description') or '')[:50]!r}")
    if len(selected) > 12:
        print(report.paint(f"  … {len(selected) - 12} more", "dim"))

    if args.dry_run:
        print(report.paint("\nDRY RUN — nothing changed.", "yellow"))
        return 0
    if not args.yes and not _confirm(f"\nClear the note on {len(selected)} playlist(s)?"):
        print("Aborted.")
        return 1

    selected, gone = metadata.alive(session, selected)
    if gone:
        print(
            report.paint(
                f"{len(gone)} playlist(s) no longer exist and were skipped.", "dim"
            )
        )
        snapshot.drop({p["id"] for p in gone})
    if not selected:
        print("Nothing left to change.")
        return 0

    result = metadata.clear_notes(
        session, selected, replacement=args.set or "", workers=args.workers
    )
    print()
    print(report.paint(f"{result['changed']} note(s) cleared.", "green"))
    if result["backup"]:
        print(report.paint(f"Previous notes saved to {result['backup']}", "dim"))
        print(report.paint("Undo with: notes --restore <that file>", "dim"))
    vanished_ids = _report_failures(result, "note change(s)")
    if vanished_ids:
        snapshot.drop(vanished_ids)

    failed_ids = {f["playlist_id"] for f in result["failed"]}
    snapshot.patch(
        {
            p["id"]: {"description": args.set or ""}
            for p in selected
            if p["id"] not in failed_ids
        }
    )
    return 0


def cmd_visibility(args: argparse.Namespace) -> int:
    session = auth.login()
    snap, entries, playlists = _entries(session, args.refresh, args.workers)
    rows = _pick(entries, playlists, args, by_name=True)

    public = [p for p in rows if p.get("public")]
    private = [p for p in rows if not p.get("public")]
    print(
        report.paint(
            f"\n{len(rows)} playlists · {len(public)} public · {len(private)} private",
            "bold",
        )
    )

    if args.public is None:
        for playlist in public[: args.show]:
            print(report.paint(f"  public   {playlist['name'][:60]}", "yellow"))
        if len(public) > args.show:
            print(report.paint(f"  … {len(public) - args.show} more public", "dim"))
        print(report.paint("\nTo change: --private (or --public)", "dim"))
        return 0

    target = args.public
    todo = [p for p in rows if bool(p.get("public")) is not target]
    word = "public" if target else "private"
    if not todo:
        print(f"All {len(rows)} already {word}.")
        return 0

    print(report.paint(f"\n{len(todo)} playlist(s) would become {word}:", "bold"))
    for playlist in todo[:15]:
        print(f"  {playlist['name'][:60]}")
    if len(todo) > 15:
        print(report.paint(f"  … {len(todo) - 15} more", "dim"))

    if args.dry_run:
        print(report.paint("\nDRY RUN — nothing changed.", "yellow"))
        return 0
    if target:
        # Going public exposes these playlists on your profile; always say so.
        print(
            report.paint(
                "\nThis makes them visible to anyone with your profile link.", "yellow"
            )
        )
    if not args.yes and not _confirm(f"\nMake {len(todo)} playlist(s) {word}?"):
        print("Aborted.")
        return 1

    rows, gone = metadata.alive(session, rows)
    if gone:
        print(
            report.paint(
                f"{len(gone)} playlist(s) no longer exist and were skipped.", "dim"
            )
        )
        snapshot.drop({p["id"] for p in gone})
    todo = [p for p in rows if bool(p.get("public")) is not target]
    if not todo:
        print("Nothing left to change.")
        return 0

    result = metadata.apply_visibility(session, rows, target, workers=args.workers)
    print()
    print(report.paint(f"{result['changed']} playlist(s) are now {word}.", "green"))
    if result["already"]:
        print(report.paint(f"{result['already']} were already {word}.", "dim"))
    vanished_ids = _report_failures(result, "visibility change(s)")
    if vanished_ids:
        snapshot.drop(vanished_ids)

    failed_ids = {f["playlist_id"] for f in result["failed"]}
    snapshot.patch(
        {p["id"]: {"public": target} for p in todo if p["id"] not in failed_ids}
    )
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    backups = actions.list_backups()
    if args.list or not args.backup:
        if not backups:
            print("No backups yet.")
            return 0
        for path in backups:
            data = json.loads(path.read_text())["playlist"]
            print(f"{path.name}\n    {data['name']} — {len(data.get('tracks') or [])} tracks")
        print(f"\n{len(backups)} backup(s).")
        return 0

    path = Path(args.backup)
    if not path.exists():
        matches = [p for p in backups if args.backup in p.name]
        if len(matches) != 1:
            print(
                f"'{args.backup}' matched {len(matches)} backups. Use --list.",
                file=sys.stderr,
            )
            return 1
        path = matches[0]

    session = auth.login()
    result = actions.restore(session, path)
    print(
        f"Recreated '{result['name']}' with {result['tracks_added']} tracks "
        f"(playlist {result['new_playlist_id']})."
    )
    return 0


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tidalcleanup",
        description="Tidy up a Tidal library: turn playlists that are really just "
        "albums into saved albums, triage the rest, upgrade albums to Dolby Atmos, "
        "scrub advertising from playlist descriptions, and change visibility in bulk.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--refresh", action="store_true", help="re-fetch from Tidal first")
        p.add_argument(
            "--workers",
            type=int,
            default=fetch.DEFAULT_WORKERS,
            help=f"parallel requests (default {fetch.DEFAULT_WORKERS})",
        )
        return p

    sub.add_parser("login", help="authenticate with Tidal").set_defaults(func=cmd_login)
    sub.add_parser("logout", help="forget the stored session").set_defaults(func=cmd_logout)

    audit = common(sub.add_parser("audit", help="classify every playlist (read-only)"))
    audit.add_argument(
        "--sample", type=int, default=5, help="example rows to print per tier"
    )
    audit.add_argument("--json", action="store_true", help="machine-readable output")
    audit.set_defaults(func=cmd_audit)

    apply_cmd = common(sub.add_parser("apply", help="convert a whole tier in one go"))
    apply_cmd.add_argument(
        "--tier", default="A", help="tiers to apply, e.g. A or AB (never D)"
    )
    apply_cmd.add_argument("--yes", action="store_true", help="skip the confirmation")
    apply_cmd.add_argument("--dry-run", action="store_true", help="rehearse only")
    apply_cmd.add_argument("--limit", type=int, help="apply at most N (good for a first batch)")
    apply_cmd.add_argument(
        "--keep-playlists", action="store_true", help="save albums but delete nothing"
    )
    apply_cmd.add_argument(
        "--no-artists", action="store_true", help="do not also follow the album artist"
    )
    apply_cmd.add_argument(
        "--skip-stale-check",
        action="store_true",
        help="don't re-check playlists for edits made since the audit",
    )
    apply_cmd.set_defaults(func=cmd_apply)

    review = common(sub.add_parser("review", help="step through playlists one at a time"))
    review.add_argument("--tier", default="C", help="tiers to review (default C)")
    review.add_argument(
        "--sample", type=int, help="review N at random from the tier (spot-check)"
    )
    review.add_argument("--limit", type=int, help="stop after N")
    review.add_argument("--dry-run", action="store_true", help="rehearse only")
    review.add_argument("--keep-playlists", action="store_true", help="never delete")
    review.add_argument("--no-artists", action="store_true", help="don't follow artists")
    review.set_defaults(func=cmd_review)

    tri = common(sub.add_parser("triage", help="keep/delete playlists, one keypress each"))
    tri.add_argument("--tier", help="only playlists in these tiers, e.g. DF")
    tri.add_argument(
        "--order", default="name", choices=triage.ORDERS, help="order to present them in"
    )
    tri.add_argument("--limit", type=int, help="stop after N playlists")
    tri.add_argument("--redo", action="store_true", help="ignore earlier decisions")
    tri.add_argument(
        "--commit", action="store_true", help="skip the loop and apply saved decisions"
    )
    tri.add_argument("--status", action="store_true", help="show saved decisions and exit")
    tri.add_argument("--dry-run", action="store_true", help="rehearse only")
    tri.add_argument("--yes", action="store_true", help="skip the confirmation")
    tri.add_argument(
        "--skip-stale-check", action="store_true", help="don't re-check for recent edits"
    )
    tri.set_defaults(func=cmd_triage)

    atm = common(sub.add_parser("atmos", help="upgrade saved albums to Dolby Atmos"))
    atm.add_argument("--apply", action="store_true", help="actually perform the swaps")
    atm.add_argument(
        "--review",
        action="store_true",
        help="step through the candidates needing a decision, one keypress each",
    )
    atm.add_argument(
        "--keep-stereo", action="store_true", help="add Atmos without removing stereo"
    )
    atm.add_argument("--limit", type=int, help="upgrade at most N albums")
    atm.add_argument(
        "--include-retitled",
        action="store_true",
        help="also apply editions matched by tracklist under a different title",
    )
    atm.add_argument(
        "--max-loss",
        type=int,
        metavar="N",
        help="also upgrade when the Atmos edition is missing at most N of your "
        "tracks (those tracks are listed and logged before anything happens)",
    )
    atm.add_argument(
        "--titles-only",
        action="store_true",
        help="skip tracklist matching entirely; require matching titles",
    )
    atm.add_argument("--dry-run", action="store_true", help="rehearse only")
    atm.add_argument("--yes", action="store_true", help="skip the confirmation")
    atm.add_argument(
        "--show-rejected",
        action="store_true",
        help="list Atmos editions rejected for missing tracks",
    )
    atm.set_defaults(func=cmd_atmos)

    notes = common(sub.add_parser("notes", help="inspect or scrub playlist notes"))
    notes.add_argument("--clear", action="store_true", help="clear the selected notes")
    notes.add_argument("--match", help="select playlists whose note contains this text")
    notes.add_argument("--all", action="store_true", help="select every playlist with a note")
    notes.add_argument("--set", help="replace with this text instead of clearing")
    notes.add_argument("--tier", help="restrict to these tiers")
    notes.add_argument("--show", type=int, default=10, help="distinct notes to print")
    notes.add_argument("--dry-run", action="store_true", help="rehearse only")
    notes.add_argument("--yes", action="store_true", help="skip the confirmation")
    notes.add_argument("--restore", help="restore notes from a backup file")
    notes.add_argument("--list-backups", action="store_true", help="list note backups")
    notes.set_defaults(func=cmd_notes)

    vis = common(sub.add_parser("visibility", help="make playlists public or private"))
    group = vis.add_mutually_exclusive_group()
    group.add_argument(
        "--private", dest="public", action="store_false", default=None,
        help="make the selection private",
    )
    group.add_argument(
        "--public", dest="public", action="store_true", default=None,
        help="make the selection public",
    )
    vis.add_argument("--match", help="only playlists whose name contains this text")
    vis.add_argument("--tier", help="restrict to these tiers")
    vis.add_argument("--show", type=int, default=15, help="rows to print")
    vis.add_argument("--dry-run", action="store_true", help="rehearse only")
    vis.add_argument("--yes", action="store_true", help="skip the confirmation")
    vis.set_defaults(func=cmd_visibility)

    restore = sub.add_parser("restore", help="rebuild a playlist from a backup")
    restore.add_argument("backup", nargs="?", help="backup filename (or a fragment)")
    restore.add_argument("--list", action="store_true", help="list available backups")
    restore.set_defaults(func=cmd_restore)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
