# Plan: Server-authoritative log mirror with agent-driven rotation

## Goal

The live mirror on the server should, at all times, hold for each tailed file
either:

- `<basename>.log` — the **current** (still-growing) segment, or
- the same name `lh-logrotate` currently has that segment under **on disk**,
  with the `.lh-logrotate-*`, `.xz`, and `.gpg` suffixes stripped.

Because `lh-logrotate` exposes both `<iso_dt>` and `<sha13>` in its own on-disk
filenames as a segment moves through its lifecycle, "strip the suffixes" yields:

| `lh-logrotate` on-disk name                                  | Mirror name                          |
|--------------------------------------------------------------|--------------------------------------|
| `foo.log` (live)                                             | `foo.log`                            |
| `foo.log.<iso_dt>.lh-logrotate-waiting`                     | `foo.log.<iso_dt>`                   |
| `foo.log.<iso_dt>.<sha13>.xz.gpg.lh-logrotate-compressed`   | `foo.log.<iso_dt>.<sha13>`           |
| `foo.log.<iso_dt>.<sha13>.xz.gpg.lh-logrotate-uploaded`     | `foo.log.<iso_dt>.<sha13>`           |
| (removed)                                                   | `foo.log.<iso_dt>.<sha13>` (kept)    |

So `basename --suffix=.xz` of the eventual S3-archived copy equals the live
mirror path. The agent **parrots** `lh-logrotate`'s `<iso_dt>` and `<sha13>` —
it never invents them — so the names match byte-for-byte even if the mirrored
content and the archived content differ.

The trailing component of a mirror name is therefore always one of: nothing
(live), `<sha13>` (finalized), or the literal `orphan` (no `lh-logrotate`
counterpart, below).

## Core principle

**The agent is the sole authority on file identity and rotation. The server
never decides identity from content.** The server only ever:

1. appends bytes to an **agent-named** destination file at an agent-specified
   offset, and
2. renames a destination file when the agent tells it to.

Each connection is bound to one explicit destination filename that the agent
chooses. The server never picks a file by matching content. The content prefix
is retained **only as an integrity check**, never as a routing or rotation
decision.

## Connection model: one connection per *segment*, agent-named target

The bug we are removing comes from two connections landing on the same
destination name and the server arbitrating between them by content. v2 removes
the arbitration, not the concurrency:

- The `hello` handshake carries an explicit **`target`** filename. The server
  resolves the directory from `path` and writes to `<dir>/<target>`. For a live
  file `target = foo.log`; for a closing segment `target = foo.log.<iso_dt>`
  (later `…<sha13>`).
- A per-tailed-path **coordinator** owns the segment connections. In steady
  state there is exactly one (the live segment). Across a rotation there are
  briefly **two**, targeting **distinct** names — the closing segment draining
  into `foo.log.<iso_dt>` and the new live segment streaming into `foo.log`.
  They never share a name, so there is nothing for the server to arbitrate.

This is the answer to "do we need multiple tasks inside the per-path handler?" —
yes, during the rotation overlap, but they write to different files, and the one
shared-name operation (the seal rename) is serialized by the agent before the
new live connection is opened.

### Why we do *not* cut over with "last byte → rename → first byte"

The producer keeps appending to the **old inode** after `lh-logrotate` renames
it, until the service reopens; `lh-logrotate` waits (`compress_delay`) for that
to settle before it hashes. So the agent cannot know the old segment is complete
at rotation time. Instead of guessing, the closing connection **stays open and
keeps draining the old inode** (now relabeled `foo.log.<iso_dt>` on the server)
concurrently with the new live segment. A `rename` relabels the file; it does
**not** stop the stream. The old inode's trailing writes keep flowing on the
closing connection into `foo.log.<iso_dt>`. The connection closes only when the
segment is provably complete (the `lh-logrotate-compressed`/`-uploaded` marker —
the authoritative "done" signal — which is also when `<sha13>` is learned).

## Rotation lifecycle (lh-logrotate-managed)

Connection **L** is the live connection (`target = foo.log`), streaming inode
`N0`. The producer rotates: `lh-logrotate` renames the old inode aside and the
service creates a new `foo.log` (inode `N1`).

1. **Detect.** `foo.log` resolves to a new inode `N1`.
2. **Learn `<iso_dt>`.** From the `…lh-logrotate-waiting` marker (wait up to
   `seal_marker_grace`; if it never appears, take the orphan path).
3. **Seal on L.** Send `rename foo.log -> foo.log.<iso_dt>` on **L**, await `ok`.
   The server renames; L's open fd now appends to `foo.log.<iso_dt>`. (L keeps
   reading inode `N0` and forwarding its trailing writes here.)
4. **Open new live connection L2.** Only **after** the seal `ok`: open L2 with
   `target = foo.log`, server creates a fresh `foo.log`, stream inode `N1` from
   offset 0. L2 becomes the live connection for the next rotation.
5. **Drain to completion on L.** Keep forwarding `N0`'s trailing bytes into
   `foo.log.<iso_dt>` until the `…lh-logrotate-compressed` (or `…-uploaded`)
   marker appears — the segment is now final and `<sha13>` is known.
6. **Finalize on L.** Send `rename foo.log.<iso_dt> -> foo.log.<iso_dt>.<sha13>`,
   await `ok`, then close L.

Steps 3 and 4 are the only ordering constraint, and it is enforced inside one
agent process: the new live connection is not opened until the seal is acked, so
`foo.log` is renamed aside exactly once, before the new one is created.

## Rotation lifecycle (orphan / non-lh-logrotate)

Files that rotate without `lh-logrotate` (services that roll their own logs,
copytruncate, …) never produce markers, so there is no `<iso_dt>`/`<sha13>` to
parrot. The agent still seals — it is the rotation authority — but with a
self-generated timestamp and a trailing `orphan` literal:

```
rename foo.log -> foo.log.<iso_dt>.orphan
```

Completion here cannot be signalled by a marker, so the closing connection
drains until the old inode is EOF-stable for `seal_idle` seconds, then closes.
`.orphan` files have no rotated/archived counterpart by definition; the trailing
literal keeps them greppable and keeps the naming rule "trailing component is
nothing / `<sha13>` / `orphan`" intact.

## Rename transport

The `rename` is an **in-band control frame on the segment's own connection** —
the same connection whose open fd holds the file being renamed:

```
A: rename 78\n
A: {"from": "foo.log.20260101T120000Z", "to": "foo.log.20260101T120000Z.bd0fe0ff5ceeb"}\n
S: ok\n
```

The server resolves the directory from the connection's `hello` header and
renames within it. The frame carries only `from`/`to` basenames.

Why in-band on the segment connection rather than the alternatives:

- **In-band (chosen).** The connection's fd survives the rename, so a relabel and
  the subsequent trailing-byte appends stay strictly ordered with no extra
  machinery. The one cross-connection ordering that matters (seal before new
  live) is a single `await` in the coordinator.
- **Dedicated control connection (rejected).** Reintroduces "which lands first
  at the server, the rename or the next data frame", and duplicates auth.
- **Out-of-band endpoint (rejected).** More parts, separate auth surface, still
  races the data path.

The frame is **idempotent**: if `from` is absent and `to` already exists, the
server replies `ok`. This is what makes restart/replay safe.

## Protocol frames

1. **`hello`** — handshake, now with an explicit target.
   ```
   A: logline-agent-v1 <n>\n
   A: {"hostname": ..., "path": ..., "target": "foo.log",
       "auth": {...}, "prefix": {"length": L, "sha1": ...}}\n
   S: ok <m>\n
   S: {"length": <target length>, "prefix_sha1": <or null>}\n
   ```
   The server opens `<dir>/<target>` if it exists and **reports** its length and
   prefix hash. It never rotates. The agent compares the reported `prefix_sha1`
   with the inode it intends to stream and decides what to do (resume, or seal a
   stale file first — see Restart).

2. **`data`** — append. The server **must** verify `offset == current length of
   the target` and raise an explicit protocol error on mismatch (not an
   `assert`). This is the only integrity gate on the append path.

3. **`rename`** — as above; idempotent.

## Marker watcher

A per-parent-directory watcher (periodic `scandir` on the tail cadence, or
inotify `MOVED_TO`/`CREATE`) feeds the coordinator two facts it must not compute
itself:

- `<iso_dt>` — from `foo.log.<iso_dt>.lh-logrotate-waiting`.
- `<sha13>` — from `foo.log.<iso_dt>.<sha13>.xz.gpg.lh-logrotate-{compressed,uploaded}`
  (the `uploaded` form is the fallback when the agent missed `compressed`).

## Server behaviour summary

- Resolve `dir = <dest>/<hostname>/<'~'.join(dir_parts)>`, create the subtree as
  needed (`mkdir(parents=True, exist_ok=True)`).
- `hello`: open `<dir>/<target>` if present, report `{length, prefix_sha1}`.
  Never rotate.
- `data`: verify offset, append, flush, ack.
- `rename`: idempotent rename of `<dir>/<from>` to `<dir>/<to>`, ack.
- No content-based rotation anywhere; the prefix is reported/verified only.

## fsync on seal (durability)

Today the server only `flush()`es after each write — that pushes bytes from the
Python buffer into the OS page cache, but not to disk, and a `rename` is a
metadata change that is likewise not durable until the directory is synced. On a
server crash/power loss you can therefore lose (a) recently appended bytes and
(b) a rename, so a "sealed" segment could reappear under its old name or be lost.

Proposal: **`fsync` only at seal/finalize, never per append.**

- On the seal and finalize renames, `os.fsync(fd)` the segment file (its bytes
  are final) and `os.fsync` the **parent directory** (so the rename itself is
  durable). One pair of fsyncs per rotation — infrequent, so the write
  amplification is negligible.
- Per-append `fsync` is deliberately avoided: it would add a disk round-trip to
  every chunk for little benefit, because the live tail is already recoverable
  without it (next point).
- The un-fsynced live tail is **not** true data loss: on reconnect the agent reads
  the server's reported `length`, and if the server lost its tail the agent
  re-sends from there (it still has the bytes on the source host). So fsync's
  real job is to make **completed segments and their final names** crash-durable,
  not to protect the live tail.

Net: durability where it's cheap and matters (sealed segments), resync where
it's free (live tail). This is the recommendation; flag if you'd rather fsync
nothing (rely entirely on resync) or fsync per append (paranoid).

## Cleanup / retention

There is **no** server-side cleanup today, and v2 does not add one: pruning the
live tree stays an external concern (a `find -mtime` cron, or the S3 pipeline on
the rotated tree). The dated/hashed naming makes that trivial and safe to
express — every non-live file ends in `<sha13>` or `orphan` and carries its
`<iso_dt>`, so a retention sweep is a pure filename/mtime operation with no
coupling to the agent or server. If we later want in-server retention it can be
a standalone periodic task over the destination tree; it is intentionally out of
scope here.

## Restart and replay

State does **not** need to persist across agent restarts.

- **Pending finalize lost on crash.** On restart the watcher rescans, still sees
  the `…compressed`/`…uploaded` marker (hours-long window), reconnects to
  `target = foo.log.<iso_dt>`, and re-emits the finalize rename. Idempotent.
- **Crash mid-rotation, before the seal.** The server still has `foo.log` = old
  (`N0`) content and no `foo.log.<iso_dt>`; on disk the live file is already
  `N1`. On reconnect the agent opens `target = foo.log` intending to stream
  `N1`, but the reported `prefix_sha1` is `N0`'s → mismatch. The agent does
  **not** write; it first issues `rename foo.log -> foo.log.<iso_dt>` (from the
  marker, or orphan), then opens the new live connection. Recovery without any
  server-side guessing.
- **Crash after the seal, before opening L2.** Server has `foo.log.<iso_dt>` and
  no `foo.log`; agent reconnects the live segment fresh (length 0) and the
  closing segment by name. Both resume.
- **Replay of an applied rename** is absorbed by idempotency.

## Why this removes the data-shuffling bug

The bug needs two connections on one destination name plus content-based
arbitration. v2 gives every connection an explicit, distinct, agent-assigned
target and a server that never arbitrates by content. The closing connection
targets `foo.log.<iso_dt>` and can never touch the live `foo.log`; the single
operation that does touch `foo.log` (the seal rename) happens once, on the
closing connection, before the new live connection exists. There is no losing
connection to rename away and no prefix decision to get wrong.

## Edge cases / known limitations

- **Agent absent for an entire marker window.** If the agent is down through
  `waiting → compressed → uploaded → removed`, the markers are gone on restart;
  that segment can only be sealed `…orphan` (or with a generated `<iso_dt>` if
  even `.waiting` was missed). Surfaced by the cross-check job, not silently
  wrong.
- **copytruncate** (same inode, truncated in place): no inode change, so the
  agent must also treat a file *shrink* as a rotation of the current segment.
- **Multiple rapid rotations.** Each opens its own closing connection under the
  coordinator; they drain independently into their own dated names.
- **Mirror lag during `seal_marker_grace`.** The new live segment is not opened
  until `<iso_dt>` is known (so the seal can name the closing file). This is a
  brief, bounded lag; new bytes wait safely on disk in the meantime.

## Timers / tunables

For `lh-logrotate`-managed files there are **no timing heuristics**: sealing is
triggered by the `.waiting` marker and completion/finalization by the
`.compressed`/`.uploaded` marker. Timers matter only in two narrow spots:

- **`seal_marker_grace = 10s`** — after a new inode is detected, how long to wait
  for `.waiting` before treating the rotation as an orphan. `lh-logrotate`
  creates `.waiting` *as* the rotation, so it is almost always already on disk
  and the real wait is ~0; the 10s is headroom for scan/visibility jitter. This
  value also bounds how long the new live segment is delayed during a normal
  rotation, so it is kept small.
- **`seal_idle` = reuse `rotated_files_inactivity_threshold` (600s)** — orphan
  only: no-growth window before the closing connection gives up and closes. A
  longer value is strictly safer (the connection keeps draining trailing writes
  the whole time; the only cost of waiting is a lingering fd, never data), so we
  fold it into the existing 600s inactivity knob rather than adding a new one.

All other decisions are settled: 2-phase naming
(`foo.log.<iso_dt>` → `foo.log.<iso_dt>.<sha13>`), orphan as a trailing literal
(`foo.log.<iso_dt>.orphan`), and fsync (file + parent dir) at seal/finalize
only.

## Implementation order

1. Server: explicit `target` in `hello` + `{length, prefix_sha1}` reporting (no
   rotate), explicit offset check, idempotent `rename`, fsync on rename. Tests:
   create + append + rename (fd survives) + re-rename + replay + offset mismatch.
2. Agent: per-path coordinator with explicit-target segment connections; seal
   (rename then open new live) ordering. Tests: clean rotation, concurrent
   closing+live streams to distinct names, no-write-before-seal recovery.
3. Agent: marker watcher feeding `<iso_dt>`/`<sha13>`; drain-to-marker
   completion and the finalize rename; `…uploaded` fallback.
4. Agent: orphan path for markerless rotation (EOF-stable completion).
5. End-to-end: real `lh-logrotate` against a stub uploader; assert the live
   mirror name equals the rotated name minus `.xz`.
6. Rollout: deploy the (backward-compatible reporting) server before the agent.
