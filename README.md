# Tidal Cleanup

_Thanks to the maintainers of tidalapi, which handles authentication:_
_https://github.com/tamland/python-tidal_

## Description

A command line tool for tidying up a Tidal music library.

When a library is moved to Tidal from another service, the transfer tools
available do the move by creating playlists. An album you had saved in Spotify
usually arrives as a playlist named after it, holding its tracks in order,
rather than as a saved album. Do that with a few hundred albums and the result
is a Tidal account where the album collection is nearly empty and the playlist
list is thousands of entries long, most of which are albums in disguise.

Sorting that out by hand is slow, and risky: the playlists worth keeping, the
ones that were actually made rather than imported, are mixed in among the
duplicates and look no different in the Tidal interface.

This tool separates the two. It reads every playlist in an account, works out
which ones are really just albums, saves those albums properly and deletes the
redundant playlists, and leaves everything else alone. It also does four
related jobs that come up while cleaning a library of that size:

* Stepping through playlists one at a time to decide what to keep, with one
  keypress per playlist, which is considerably faster than the web interface.
* Finding saved albums that also exist as Dolby Atmos releases, and swapping
  them in without losing any tracks.
* Removing the advertising that transfer tools write into playlist
  descriptions.
* Changing playlists between public and private in bulk.

Nothing is deleted without a confirmation. Every playlist is written to disk
before anything happens to it, and a `restore` command rebuilds one from that
copy.

## Requirements

* Python 3.8 or newer, which is tidalapi's own requirement. Development and
  testing were done on Python 3.14.
* A Tidal account. A paid subscription is needed for the Dolby Atmos features,
  since Atmos releases are not available on the free tier.

The only dependency is tidalapi. It is not bundled; the installation step
below fetches it.

## Installation

    git clone https://github.com/tobiastoft/tidal-cleanup.git
    cd tidal-cleanup
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt

The `tidalcleanup` script in the project root runs the tool from that virtual
environment, so there is nothing to activate:

    ./tidalcleanup --help

## Logging in

    ./tidalcleanup login

This prints a tidal.com link. Open it, approve the request, and the session is
written to `~/.config/tidal-cleanup/session.json` with file mode 600. It is
read from there on every later run, so logging in is a one-time step.

The session file is the only credential involved, and it is kept outside the
project directory so it cannot be committed by accident. `./tidalcleanup
logout` deletes it.

## How to use

Start by having a look at what is there. This reads the account and writes
nothing back to Tidal:

    ./tidalcleanup audit

Everything the audit reads is cached in `data/snapshot.json`, so the later
commands work from that local copy and run immediately. The audit sorts every
playlist into one of six tiers:

| Tier | What it means |
| --- | --- |
| A | Every track is on one album, at least 85% of that album is present, and the playlist is named after it |
| B | The same evidence from the tracks, but the playlist has been renamed by hand |
| C | The playlist holds tracks the album does not, or less than 70% of the album |
| D | The tracks are genuinely spread across many albums, so this is a real playlist |
| E | The playlist looks like an album, but that album is no longer on Tidal |
| F | Empty, or a single track |

The verdict comes from comparing track titles against the album's real track
list, not from the playlist's name. That matters in both directions: an album
playlist that was renamed at some point is still recognised, and a playlist
that merely happens to be named after an album is still protected. A playlist
containing even one track that is not on the matched album is kept out of tiers
A and B, because deleting it would lose that track.

Tier A is the tier that can be acted on without reading every entry. Spot-check
a sample of it first:

    ./tidalcleanup review --tier A --sample 20

That shows twenty at random, one screen each, with the evidence behind the
verdict. Press `s` to move on, or `d` to see the full track list and which
album tracks are missing. When the sample looks right, convert the whole tier:

    ./tidalcleanup apply --tier A --dry-run
    ./tidalcleanup apply --tier A

The dry run reports what would happen and changes nothing. The real run asks
for confirmation once for the entire tier, saves each album, follows its
artist, and then deletes the playlist. Add `--limit 20` to do a small first
batch.

Tiers B and C are worth going through individually, since the evidence for them
is weaker:

    ./tidalcleanup review --tier C

NOTE: `apply` refuses to act on tier D. Those are the playlists the whole tool
exists to protect.

### Deciding what to keep

The playlists left after the albums have been converted are the ones that need
a judgement rather than a rule. `triage` presents them one at a time and takes
a single keypress each, with no need to press Enter:

    ./tidalcleanup triage
    ./tidalcleanup triage --order size      # largest first
    ./tidalcleanup triage --tier DF         # only real playlists and empty ones

| Key | Action |
| --- | --- |
| `d` | mark for deletion |
| `k` | keep, and exempt it from album conversion later |
| `s` | skip and decide another time |
| `u` | undo the last decision |
| `a` | save the album and delete the playlist, offered only when it is an album |
| `t` | list every track |
| `o` | open the playlist in Tidal |
| `q` | stop and review what has been marked |

Nothing is deleted while decisions are being made. Each keypress is written to
`data/triage.json`, so stopping and resuming later costs nothing and a resumed
run skips what has already been decided. The deletions happen afterwards as a
single confirmed batch. `--status` shows what is currently marked and
`--commit` applies it without stepping through again.

Each screen carries enough to judge a playlist without opening Tidal: how many
tracks and how many distinct artists it holds, its duration, how many duplicate
tracks are in it, a warning when other playlists share its name, its most
frequent artists, and six tracks sampled across the whole playlist rather than
the first six, since imported playlists often repeat the same track near the
top.

### Upgrading albums to Dolby Atmos

Tidal publishes a Dolby Atmos release as a separate album with its own
identifier, usually under the same title as the stereo one. `atmos` finds those
and swaps them in:

    ./tidalcleanup atmos                    # report only
    ./tidalcleanup atmos --apply            # take the clean upgrades
    ./tidalcleanup atmos --review           # step through the judgement calls

A swap only happens when every track on the saved album is also on the Atmos
edition, so an upgrade cannot cost coverage. An Atmos release that is missing
even one track is reported separately, with the missing tracks named, rather
than being applied. `--max-loss 1` opts into accepting a loss of at most one
track per album, and names every track it would give up before doing anything.

Candidates that need a decision — an edition published under a different title,
or one that would cost a track — can be stepped through with `--review`, using
`y` to accept, `n` to skip, `t` to compare both track lists and `o` to open
both albums in Tidal.

Please note that finding Atmos editions means listing each artist's full
catalogue, because searching for the album by name does not reliably return
them. The results are cached, so the first run takes a few minutes and later
runs take seconds.

### Removing advertising from playlist descriptions

Transfer tools commonly write a line of advertising for themselves into the
description of every playlist they create. `notes` finds and removes it:

    ./tidalcleanup notes                                 # group the notes found
    ./tidalcleanup notes --clear --match tunemymusic      # clear only those
    ./tidalcleanup notes --restore <file>                 # put them back

The first form groups identical descriptions together, so a tool's boilerplate
appears as one line with a count next to it rather than once per playlist.
`--match` takes a case-insensitive substring, which is how a particular
advertisement can be targeted while leaving hand-written descriptions alone.
Clearing without either `--match` or `--all` is refused. The previous
descriptions are saved to `data/notes/` first, and `--restore` writes them back.

### Changing visibility in bulk

    ./tidalcleanup visibility                        # how many are public, and which
    ./tidalcleanup visibility --private              # make the selection private
    ./tidalcleanup visibility --public --match mix   # or a subset, by name

Playlists already in the requested state are skipped rather than rewritten.
Making playlists public prints a warning that they become visible to anyone
with the account's profile link, and asks before proceeding.

### Undoing a deletion

    ./tidalcleanup restore --list
    ./tidalcleanup restore <backup-file>

Every playlist deleted by this tool was written to `data/backups/` first, with
its name, description and every track. `restore` recreates it from that file.

## Keeping the local copy current

The snapshot in `data/` is a copy of how the account looked when `audit` last
ran. It goes out of date as soon as something is changed in the Tidal app
itself. Re-read it with:

    ./tidalcleanup audit --refresh

`--refresh` is accepted by every command that reads the snapshot, so it can be
folded into whatever is being done rather than run as a separate step:

    ./tidalcleanup notes --refresh --clear --match tunemymusic

It is a cheap operation. Only the list of playlists is fetched again; the track
list of a playlist that has not changed is taken from the cache, and album
details are cached permanently.

## What is kept on disk

Everything the tool writes locally lives in `data/`, which is excluded from
version control. None of it needs to be kept; deleting the directory loses the
backups and the record of what was done, and everything else is rebuilt on the
next `audit --refresh`.

    data/snapshot.json   how the account looked when audit last ran
    data/cache.json      track lists and album details, so re-runs are cheap
    data/plan.tsv        one row per playlist: tier, evidence, matched album
    data/backups/        a full copy of every playlist before it was deleted
    data/notes/          descriptions as they were before being cleared
    data/triage.json     keep and delete decisions, saved as they are made
    data/actions.jsonl   an append-only record of everything that changed

## Safety

Deleting several hundred playlists on the strength of a guess would be a poor
trade, so the destructive paths are all guarded:

* `audit`, and `notes` and `visibility` without an action flag, only read.
  They write nothing back to Tidal.
* Every playlist is copied to `data/backups/` before anything is done to it.
* A playlist is only deleted after its album is confirmed present in the
  collection. If saving the album fails, the playlist is left alone.
* Before a batch runs, the playlists involved are checked against Tidal again.
  Any that have been edited since the audit are held back, because the copy on
  disk is no longer accurate, and any that have already been deleted are
  skipped rather than reported as errors.
* Only playlists created by the logged-in account are ever listed, so a
  playlist belonging to somebody else cannot be affected.
* Everything that changes is appended to `data/actions.jsonl`, including what
  an Atmos swap cost in tracks.

## Tests

    for t in tests_*.py; do .venv/bin/python $t; done

There are 186 assertions across five files, covering the classifier, a
simulated account of a thousand playlists, the interactive loops driven by
scripted keypresses, Atmos matching, and the description and visibility
changes. They use a stand-in for the Tidal API, so they need no network access
and no account.

## A caveat

This talks to Tidal's internal API, by way of tidalapi, rather than their
official developer API. That is a deliberate choice: the official API cannot
delete a user's playlists, which is most of the point here. The consequence is
that a change at Tidal's end could break this tool without warning. It is also
worth saying plainly that a tool whose job is to delete things in bulk deserves
a cautious first run. Use `--dry-run`, and `--limit` on the first real batch.

[NOTES.md](NOTES.md) describes how the matching works in more detail, along
with the parts of the Tidal API that behave unexpectedly, in case either is
useful to anyone reading the source.
