Logline Agent - Server Protocol
===============================

Agent connects to the Server using TCP connection.
Default port number 5645 – this number was randomly chosen, not assigned by anybody.

The connection can be wrapped into TLS – in that case both Agent and Server must be configured to use TLS.

Purpose of the connection is to transfer a log file content from Agent to Server.

The agent is the sole authority on file identity and rotation. Each connection is
bound to one explicit **`target`** filename chosen by the agent; the server only
appends bytes to that target and renames it when told to. It never decides file
identity from content – the content prefix is reported/verified only as an
integrity check, never used for routing or rotation.

Framing
-------

Every message starts with an ASCII command line
`<command> <metadata-length> [<data-length>]\n`, followed by `<metadata-length>`
bytes of JSON metadata and, when `<data-length>` is present, that many bytes of
raw payload. The agent ends its JSON metadata with `\n`, which is counted in
`<metadata-length>`.

The server answers every message with a reply line: either `ok\n`, or
`ok <length>\n` followed by exactly `<length>` bytes of JSON (without a trailing
newline).

On any error – failed authentication, an invalid header, an offset mismatch, a
malformed frame, and so on – the server logs it and closes the connection
**without sending a reply**. It also closes a new connection that does not send
its first command within 30 seconds. The agent treats a closed connection as a
failure and reconnects; the handshake reply then tells it where to resume. (The
agent also understands an `error` reply line, but the server never sends one.)

Handshake
---------

```
Agent (A) connects to Server (S)
A: logline-agent-v1 186\n
A: {"hostname": "server.example.com", "directory": "/var/log", "target": "something.log", "prefix": {"length": 42, "sha1": "aTQsXDnlrl8Ad67MMsD4GBH7gZM="}, "auth": {"client_token": "..."}}\n
S: ok 62\n
S: {"length": 195, "prefix_sha1": "aTQsXDnlrl8Ad67MMsD4GBH7gZM="}
```

The agent sends the source `directory` and the destination leaf `target`
separately. The server maps `directory` to `<dest>/<hostname>/<mangled-directory>`
(each `/` becomes `~`) and writes to `<that directory>/<target>`; it never derives
a filename from content. It opens the target if it exists and **reports** its
current `length` and the SHA-1 (base64) of its first `prefix.length` bytes as
`prefix_sha1` (`length` 0 and `prefix_sha1` `null` if the target does not exist
or is empty). The server never rotates. The agent compares `prefix_sha1` with the
inode it intends to stream and decides what to do (resume, or seal a stale
target aside first). A handshake without a valid `target` is rejected.

The handshake does not create the target. The server creates it on the first
`data` frame, which must then have `offset` 0.

Data
----

```
A: data 37 44\n
A: {"offset": 195, "compression": null}\n
A: now the Agent sends the raw log file content
S: ok\n
```

The server **must** verify that `offset` equals the current length of the target;
on mismatch it closes the connection (see [Framing](#framing)).
`compression` may be `null`, `"gzip"`, `"lzma"` or `"zst"`.

Rename
------

An in-band control frame on the segment's own connection – the same connection
whose open fd holds the file being renamed. The open fd survives the rename, so a
relabel and the subsequent trailing-byte appends stay strictly ordered.

```
A: rename 85\n
A: {"from": "foo.log.20260101T120000Z", "to": "foo.log.20260101T120000Z.bd0fe0ff5ceeb"}\n
S: ok\n
```

The server resolves the directory from the connection's `hello` header and renames
within it. The frame carries only `from`/`to` basenames and is **idempotent**: if
`from` is absent and `to` already exists, the server replies `ok` (this makes
restart/replay safe). On the rename the server fsyncs the segment file and the
parent directory so completed segments and their final names are crash-durable.

Rotation naming
---------------

Across a rotation there are briefly two connections targeting **distinct** names:
the closing segment draining the old inode into `foo.log.<iso_dt>` and the new live
segment streaming into `foo.log`. The agent parrots lh-logrotate's `<iso_dt>` and
`<sha13>` from the file names it finds in the log's directory, so the finalized
mirror name equals lh-logrotate's archived name minus the `.xz.gpg` suffix.
lh-logrotate renames the rotated log to `foo.log.<iso_dt>.lh-logrotate-waiting`,
which gives the agent `<iso_dt>`; a later
`foo.log.<iso_dt>.<sha13>.xz.gpg.lh-logrotate-compressed` (or `-uploaded`) name
gives it `<sha13>`. The agent only matches the names and never reads the files:

```
foo.log                                  (live segment)
foo.log.<iso_dt>                         (sealed, draining to completion)
foo.log.<iso_dt>.<sha13>                 (finalized lh-logrotate segment)
foo.log.<iso_dt>.orphan                  (markerless rotation, no archived counterpart)
foo.log.orphan                           (markerless rotation of an ephemeral source)
```

Sources configured as **ephemeral** in the agent (see the agent's `ephemeral`
config key) rotate frequently without lh-logrotate markers — Docker's
`json-file` driver, which self-rotates every few minutes, is the motivating
case. For these, a markerless rotation seals to the fixed name `foo.log.orphan`
with **no timestamp**. Because the server's rename clobbers an existing
destination (POSIX `rename`), each rotation overwrites the previous orphan, so
the server holds at most two files per source: the live `foo.log` and the last
sealed `foo.log.orphan`. Marker-based (lh-logrotate) rotations of ephemeral
sources are unaffected and keep the `<iso_dt>`/`<sha13>` naming above. This is a
purely agent-side naming choice; the server and protocol are unchanged.
