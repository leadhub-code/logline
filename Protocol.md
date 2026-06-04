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

Handshake
---------

```
Agent (A) connects to Server (S)
A: logline-agent-v1 153\n
A: {"hostname": "server.example.com", "directory": "/var/log", "target": "something.log", "prefix": {"length": 42, "sha1": "aTQsXDnlrl8Ad67MMsD4GBH7gZM="}, "auth": {"client_token": "..."}}\n
S: ok 44\n
S: {"length": 195, "prefix_sha1": "aTQsXDnlrl8Ad67MMsD4GBH7gZM="}\n
```

The agent sends the source `directory` and the destination leaf `target`
separately. The server maps `directory` to `<dest>/<hostname>/<mangled-directory>`
(each `/` becomes `~`) and writes to `<that directory>/<target>`; it never derives
a filename from content. It opens the target if it exists and **reports** its
current `length` and the SHA-1 (base64) of its first `prefix.length` bytes as
`prefix_sha1` (`null` if the target does not exist). The server never rotates. The
agent compares `prefix_sha1` with the inode it intends to stream and decides what
to do (resume, or seal a stale target aside first). A handshake without a valid
`target` is rejected.

Data
----

```
A: data 37 44\n
A: {"offset": 195, "compression": null}\n
A: now the Agent sends the raw log file content
S: ok\n
```

The server **must** verify that `offset` equals the current length of the target
and replies with an explicit protocol error (closing the connection) on mismatch.
`compression` may be `null`, `"gzip"`, `"lzma"` or `"zst"`.

Rename
------

An in-band control frame on the segment's own connection – the same connection
whose open fd holds the file being renamed. The open fd survives the rename, so a
relabel and the subsequent trailing-byte appends stay strictly ordered.

```
A: rename 78\n
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
`<sha13>` from its on-disk marker files, so the live mirror name equals the
eventual archived name minus the `.xz`/`.gpg` suffixes:

```
foo.log                                  (live segment)
foo.log.<iso_dt>                         (sealed, draining to completion)
foo.log.<iso_dt>.<sha13>                 (finalized lh-logrotate segment)
foo.log.<iso_dt>.orphan                  (markerless rotation, no archived counterpart)
```
