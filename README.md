# tidalcleanup

Tidy up a Tidal library from the command line. Built for a collection with hundreds
of playlists imported from Spotify.

- **Convert album-playlists into saved albums.** An import leaves behind playlists
  that are just albums (`Kurt Vile – b'lieve i'm goin down...`). Save them properly
  and delete the playlist, without touching the playlists you actually made.
- **Triage playlists by hand**, one keypress each, faster than the web UI.
- **Upgrade saved albums to Dolby Atmos** where an Atmos edition exists.
- **Scrub advertising** that import tools leave in playlist descriptions.
- **Make playlists public or private in bulk.**

Nothing is deleted without a confirmation, every playlist is backed up first, and
`restore` puts one back.

## Setup

One dependency, already installed in `.venv/`. To rebuild:

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

Then authenticate once — it prints a link to open:

    ./tidalcleanup login

## Quick start

    ./tidalcleanup audit                          # read-only: what's an album, what's yours
    ./tidalcleanup review --tier A --sample 20    # spot-check 20 of the obvious ones
    ./tidalcleanup apply --tier A                 # convert them all, one confirmation
    ./tidalcleanup review --tier C                # the uncertain few, by hand
    ./tidalcleanup triage                         # delete playlists you don't want

## Keeping the snapshot fresh

`audit` caches everything it reads into `data/snapshot.json`, so later commands are
instant. That copy goes stale as soon as you change something in the Tidal app, so
re-read it with:

    ./tidalcleanup audit --refresh

`--refresh` works on every command, so you can fold it in: `notes --refresh --clear …`.
It is cheap — only the playlist list is re-fetched, since track lists are cached per
playlist and album data is cached permanently.

## Commands

### `audit` — classify everything (read-only)

Sorts every playlist into a tier and writes `data/plan.tsv` as an audit trail.

| Tier | Meaning | Bulk-apply? |
| --- | --- | --- |
| **A** | Every track is on one album, ≥85% of it present, and the playlist is named after it | Yes |
| **B** | Same track proof, but renamed by hand | Your call |
| **C** | Holds tracks the album doesn't, or under 70% of the album | Review individually |
| **D** | Genuinely spread across albums — your own playlists | Never |
| **E** | Album-shaped, but the album is delisted from Tidal | Nothing to save |
| **F** | Empty or single-track | Nothing to convert |

The verdict rests on **track titles**, not names, so a renamed album playlist is
still found and a playlist merely *named* after an album is still protected. Any
track not on the matched album drops a playlist out of A/B, because deleting it
would lose that track. See [NOTES.md](NOTES.md) for the details.

`--sample N` examples per tier · `--json` · `--refresh`

### `apply` — convert a whole tier

    ./tidalcleanup apply --tier A --dry-run   # rehearse
    ./tidalcleanup apply --tier A --limit 20  # cautious first batch
    ./tidalcleanup apply --tier A             # the rest

Saves the album, follows the artist, then deletes the playlist. One confirmation for
the whole tier (type `yes`, not `y`).

`--tier A|AB|ABC` · `--limit N` · `--dry-run` · `--yes` · `--keep-playlists` ·
`--no-artists` · `--skip-stale-check`

### `review` — step through playlists one at a time

    ./tidalcleanup review --tier C
    ./tidalcleanup review --tier A --sample 20   # random spot-check

`y` convert · `a` save album but keep the playlist · `d` full track list and what's
missing · `s` skip · `n` never ask again · `q` quit

### `triage` — keep or delete, one keypress each

    ./tidalcleanup triage                  # everything, alphabetical
    ./tidalcleanup triage --order size     # biggest first
    ./tidalcleanup triage --tier DF        # only curated / empty
    ./tidalcleanup triage --status         # what's marked so far
    ./tidalcleanup triage --commit         # apply saved decisions

`d` delete · `k` keep (also exempts it from album conversion) · `s` skip ·
`u` undo · `a` save album and delete (only when it *is* an album) · `t` all tracks ·
`o` open in Tidal · `q` stop and review

Nothing is deleted while you decide — each keypress is saved to `data/triage.json`,
so quitting and resuming is free, and a resumed run skips what you've already
decided. Deletions happen as one confirmed batch at the end.

Each screen shows what you need in order to judge without opening Tidal: track and
artist counts, duration, duplicate-track count, a warning when other playlists share
the name, the most frequent artists, and six tracks sampled *across* the playlist.

### `atmos` — upgrade saved albums to Dolby Atmos

    ./tidalcleanup atmos                   # report
    ./tidalcleanup atmos --apply           # take the clean upgrades
    ./tidalcleanup atmos --review          # step through the judgement calls
    ./tidalcleanup atmos --max-loss 1      # accept losing at most 1 track

Tidal publishes an Atmos release as a *separate album* with its own id, so this
lists each artist's catalogue to find them. **Coverage is never lost:** a swap
happens only when every track on your saved album is also on the Atmos edition. The
stereo album is removed only after the Atmos one is confirmed saved.

Candidates needing a decision — a retitled edition, or one that would cost you a
track — are listed separately. `--review` walks them with `y` accept · `n` skip ·
`t` both tracklists · `o` open both · `q` stop.

`--apply` · `--review` · `--max-loss N` · `--include-retitled` · `--titles-only` ·
`--keep-stereo` · `--limit N` · `--show-rejected` · `--dry-run` · `--yes`

### `notes` — scrub playlist descriptions

    ./tidalcleanup notes                                # group the distinct notes
    ./tidalcleanup notes --clear --match tunemymusic     # clear just those
    ./tidalcleanup notes --restore <file>                # undo

Groups identical notes so a tool's boilerplate shows as one line with a count.
`--match` is a case-insensitive substring, so you can target an ad and leave your own
descriptions alone. Clearing without `--match` or `--all` is refused. Previous notes
are saved to `data/notes/` first.

`--clear` · `--match TEXT` · `--all` · `--set TEXT` · `--tier` · `--restore FILE` ·
`--list-backups` · `--dry-run` · `--yes`

### `visibility` — public or private in bulk

    ./tidalcleanup visibility                        # how many are public, and which
    ./tidalcleanup visibility --private              # make them private
    ./tidalcleanup visibility --public --match ss    # or a subset by name

Playlists already in the target state are skipped. Going public warns that they
become visible to anyone with your profile link, and asks first.

`--private` / `--public` · `--match TEXT` · `--tier` · `--dry-run` · `--yes`

### `restore` — put a playlist back

    ./tidalcleanup restore --list
    ./tidalcleanup restore <backup-file>

## Safety

- `audit`, `notes` and `visibility` with no action flag never write anything.
- Every playlist is written in full to `data/backups/` — name, description, every
  track id — *before* anything happens to it. `restore` rebuilds it.
- A playlist is deleted only after its album is confirmed present in your
  collection. A failed save leaves the playlist alone.
- Before a batch, playlists are re-checked against Tidal: anything **edited since
  the audit** is held back (its backup would be stale) and anything **already
  deleted** is skipped rather than reported as an error.
- `atmos` never swaps in an edition missing a track you have, and removes the stereo
  album only after the Atmos one is confirmed saved.
- Only playlists you *created* are ever listed, so one you merely follow can't be hit.
- `apply --tier D` is refused outright.
- Every action is appended to `data/actions.jsonl`, including what an Atmos swap cost.

## Files

    data/snapshot.json   what audit read (safe to delete; rebuilt with --refresh)
    data/cache.json      track lists and album metadata, keyed for cheap re-runs
    data/plan.tsv        one row per playlist: tier, evidence, matched album
    data/backups/        full copy of every playlist before it was deleted
    data/notes/          descriptions before they were cleared
    data/triage.json     your keep/delete decisions
    data/actions.jsonl   append-only log of everything that changed
    ~/.config/tidal-cleanup/session.json   OAuth session, mode 600

## Tests

    for t in tests_*.py; do .venv/bin/python $t; done

186 assertions across five files: the classifier, a simulated 1000-playlist account,
the triage loop driven by scripted keypresses, Atmos detection and swapping, and
notes/visibility.

## Caveat

This uses `tidalapi`, which talks to Tidal's internal API rather than their official
developer API — deliberately, since the official one cannot delete user playlists.
A Tidal-side change could break it.

## Layout

    tidal_cleanup/
      auth.py        OAuth login, cached session
      fetch.py       raw paginated endpoints, thread pool, backoff, progress
      snapshot.py    resumable snapshot of every playlist
      classify.py    verdicts and tiers
      plan.py        data/plan.tsv
      report.py      terminal formatting
      actions.py     backup, favouriting, staleness guard, delete, restore
      atmos.py       finding and swapping Dolby Atmos editions
      metadata.py    note scrubbing and public/private changes
      keys.py        single-keypress input
      triage.py      the keep/delete loop
      cli.py         argument parsing and the interactive loops

[NOTES.md](NOTES.md) covers how matching works and the Tidal API quirks behind it.
