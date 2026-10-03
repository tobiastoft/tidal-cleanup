# Implementation notes

Background on how the matching works and which parts of Tidal's API misbehave.

Kept out of the README to keep that focused on use.

## How a playlist is judged

The verdict compares **track titles** against the album's real track list — not
playlist names, and not which album id each track happens to cite. Two numbers
come out of it:

- **precision** — the share of the playlist that is on the album. At 1.00, nothing
  would be lost by deleting the playlist.
- **coverage** (recall) — the share of the album the playlist holds.

Each playlist is scored against several candidate albums and matched to the one it
most nearly *equals* (best F1). That matters because a Spotify import routinely
files tracks under the wrong release: in a real library, `Tame Impala - Lonerism`
had two of its twelve tracks citing compilations called `Workout 2019` and
`Summer Playlist`, and `Strangeways, Here We Come` had all ten tracks citing a
98-track anthology. Counting album ids calls the first "15% off-album" and the
second "10% of the album"; comparing titles gets both right.

When no cited album fits, the playlist's own name is searched on Tidal and a hit is
adopted only if its titles nearly equal the playlist's **and the artist agrees** —
a full cover album (Janice Whaley's a cappella `Meat Is Murder`) has an identical
track list and otherwise scores a perfect match.

### How editions are matched

An Atmos release is rarely named identically to the album you own, so three things
have to line up. All matching happens within a single artist.

- **Base title.** Any edition or mix suffix is stripped, so `American Beauty (Atmos
  Mix)` and `American Beauty (2013 Remaster)` share the base title `american beauty`.
  The first word is never dropped, so an album genuinely called *Remaster* survives.
- **Suffix-tolerant tracklists.** Atmos releases routinely append the mix name to
  *every track* — `Black Peter` becomes `Black Peter (2023 Mickey Hart Mix)`.
  Requiring exact track titles rejected every such album (precision 0.00 with all
  tracks reported missing), so a title matches when one is the other plus a trailing
  suffix at a word boundary. Each candidate track is consumed once, so a compilation
  cannot match the same track repeatedly, and titles under four characters are never
  prefix-matched.
- **What kind of recording it is.** Within one artist a *live* album can have an
  identical tracklist to the studio one. Candidates are split: a different
  performance (live, unplugged, acoustic, demo, BBC session) is dropped outright,
  while a possible reworking (redux, revisited, remix, dub) is surfaced for review
  rather than trusted or discarded.

### Reading the rejections

A rejected candidate is usually not a missed opportunity. The tracklist route
compares your album against *every* plausible Atmos release by the same artist, so
most rejections are simply the wrong album being ruled out — on one real library, 172
of 178 rejections were that. Those are reported as "compared and discarded" and
counted, not listed.

What is worth your attention is a **near miss**: an Atmos edition of the *same* album
that is missing some of your tracks. Those are listed individually, fewest losses
first, naming every track you would lose. Usually it means you own a deluxe or box
edition and the Atmos release is the standard album.

    ./tidalcleanup atmos --review         # step through them, one keypress each
    ./tidalcleanup atmos --max-loss 1     # or accept any losing at most 1 track

`--review` walks the candidates that need a judgement — retitled matches first
(they cost nothing), then near misses ordered by how little they lose. Each screen
names the album you have, the Atmos edition, and every track you would lose:

| Key | Action |
| --- | --- |
| `y` | accept this swap |
| `n` | skip it |
| `t` | show both tracklists side by side |
| `o` | open both albums in Tidal |
| `q` | stop and apply what you accepted |

Nothing is applied while you decide; the accepted set goes through the same
confirmation, which lists the losses again before anything changes.

`--max-loss N` is the non-interactive equivalent. It is deliberately lossy, so the confirmation spells
out every track that would go, and each swap records them in `data/actions.jsonl`
under `tracks_lost`. An album that already has a complete Atmos edition is never
offered a lossy one as well.

That produces two buckets. Same base title and same kind of recording is applied by
`--apply`. Anything matched only by tracklist, or carrying a reworking marker, is
listed separately with both titles and needs `--include-retitled` — the title no
longer corroborates anything, so these additionally have to cover at least 85% of the
Atmos edition, which keeps a greatest-hits record that happens to contain your album
out of the results. `--titles-only` turns tracklist matching off entirely.

The stereo album is removed only after the Atmos edition is **confirmed present** in
your collection, so a failed save can never leave you with neither. Every swap is
written to `data/actions.jsonl` with both album ids and both track counts, which is
what you would use to reverse one.

Finding editions requires listing an artist's whole catalogue: search does not
reliably surface them (searching `Neil Young After the Gold Rush` returns only the
stereo id), and `artists/{id}/albums` needs `filter=ALL` — for one artist tested the
default returned 193 albums and 12 Atmos editions where `filter=ALL` returned 243 and
19. Artist catalogues are cached in `data/atmos.json`, so a second run is cheap.

The scan runs as four parallel, individually cached phases — saved albums, artist
catalogues, candidate pairing (no requests), then the track lists those pairs need.
Two details keep it fast, both learned the slow way:

- **Saved albums come from the favourites listing, not one fetch each.** The
  `users/{id}/favorites/albums` payload already embeds `artist`, `audioModes`,
  `mediaMetadata` and `numberOfTracks`, so 756 albums arrive in ~16 requests.
- **Artist catalogues are fetched in parallel.** Walking ~500 artists one at a time
  was the bulk of a twelve-minute run.

Together those took a full scan of 756 albums from over twelve minutes to about five
seconds. Do not run two scans at once: Tidal rate-limits per token, and concurrent
runs throttle each other badly. When throttling does happen the progress line says so
(`[rate-limited 12x, waited 60s]`) rather than appearing to hang.

Saved *tracks* are not covered — only albums. An Atmos track is a different track id
and matching them reliably needs different evidence than a title list.

## Why it doesn't use tidalapi's own helpers

It uses `tidalapi` for auth and favourites, but talks to the playlist endpoints
directly, because at this scale the library's helpers are wrong or ruinous:

- `user.playlists()` sends no `limit`/`offset`, and Tidal caps the page at 50 — it
  silently returns only your first 50 playlists.
- Its playlist parser constructs a `UserPlaylist` per row, and that constructor
  fires its own GET, so merely *listing* 1000 playlists costs 1000 extra requests.
- `favorites.add_album()` accepts a comma-separated list, so saving 850 albums is
  ~17 requests instead of 850.

A full audit of ~500 playlists is roughly 2500 requests and took ~5 minutes on a
real library (playlist listing, track lists, album metadata, album track lists, and
the search recovery pass). Re-runs are near-free: track lists are cached against
each playlist's `lastUpdated`, and album metadata, album track lists and search
results are cached permanently. An interrupted run resumes where it stopped.
