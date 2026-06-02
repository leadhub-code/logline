Logline Agent–Server Protocol (logline/2)
=========================================

The agent opens a single TCP connection (optionally wrapped in TLS) to the
server and multiplexes all of the log files it watches over that one
connection. Each watched file is a *stream* identified by a numeric
`stream_id`. The default port is 5645 (chosen at random, not assigned by
anybody).

This document describes protocol version 2 (`logline/2`). It is **not**
backwards compatible with the original line-based protocol.


Design goals
------------

- **One connection per agent**, not per file — a single handshake, many streams.
- **Pipelined transfer**: the agent streams `DATA` frames without waiting for a
  reply to each one. The server sends *cumulative* `ACK`s; the agent keeps the
  amount of un-acknowledged data below a configurable window (backpressure).
- **At-least-once, no duplicates on disk**: every `DATA` frame carries an
  absolute file offset; the server writes idempotently, dropping anything it
  already has. After a reconnect the agent simply resumes from the offset the
  server reports.
- **Durable**: the server can `fsync` on a configurable policy.
- **Liveness**: heartbeats and an idle timeout detect dead connections.


Framing
-------

Every message is a *frame* with a fixed 9-byte header followed by a payload.
All integers are unsigned big-endian (network byte order):

```
 0               1                   5                   9
 +-------+-------+-------+-------+-------+-------+-------+-------+-------+
 | type  |        stream_id (u32)        |     payload_len (u32)        |
 +-------+-------+-------+-------+-------+-------+-------+-------+-------+
 |                        payload (payload_len bytes)                  |
 +--------------------------------------------------------------------+
```

- `type` (u8): the frame type (see below).
- `stream_id` (u32): the stream this frame belongs to. `0` is reserved for
  connection-level frames (`HELLO`, `HELLO_ACK`, `HEARTBEAT`, connection `CLOSE`,
  `ERROR`).
- `payload_len` (u32): length of the payload in bytes. The server rejects frames
  whose declared length exceeds a configured maximum.

Control frames carry a UTF-8 **JSON object** as their payload. The single
exception is `DATA`, whose payload is binary (described below).


Frame types
-----------

| value | name        | direction       | payload |
|-------|-------------|-----------------|---------|
| 1     | `HELLO`     | agent → server  | `{"protocol": "logline/2", "hostname": str, "auth": {"token": str}}` |
| 2     | `HELLO_ACK` | server → agent  | `{"max_frame_size": int, "ack_interval": float}` |
| 3     | `OPEN`      | agent → server  | `{"path": str, "prefix": {"size": int, "sha256": str}}` |
| 4     | `OPEN_ACK`  | server → agent  | `{"offset": int}` |
| 5     | `DATA`      | agent → server  | binary, see below |
| 6     | `ACK`       | server → agent  | `{"offset": int}` (cumulative durable offset) |
| 7     | `HEARTBEAT` | both            | `{}` |
| 8     | `CLOSE`     | both            | `{"reason": str}` (stream-level if `stream_id != 0`) |
| 9     | `ERROR`     | server → agent  | `{"code": str, "message": str}` |


`DATA` payload
--------------

The `DATA` payload is self-describing so the body can be compressed:

```
 +-------+-------+-----------------+----------------------------------+
 |   meta_len (u32)  |  meta JSON   |              body                |
 +-------+-------+-----------------+----------------------------------+
```

- `meta_len` (u32): length of the JSON metadata.
- meta JSON: `{"offset": int, "codec": "none"|"gzip"|"deflate", "raw_size": int}`
  — `offset` is the absolute position in the source file at which `body`
  starts (after decompression); `raw_size` is the uncompressed length.
- body: the (optionally compressed) log bytes.

The agent only uses codecs available in the Python standard library
(`gzip`, `deflate`); the server additionally accepts `none`.


Connection lifecycle
---------------------

```
Agent (A) connects to Server (S)

A -> S   HELLO      stream 0   {"protocol":"logline/2","hostname":"web1","auth":{"token":"…"}}
S -> A   HELLO_ACK  stream 0   {"max_frame_size":4194304,"ack_interval":0.5}

# For each watched file the agent opens a stream:
A -> S   OPEN       stream 1   {"path":"/var/log/app.log","prefix":{"size":256,"sha256":"…"}}
S -> A   OPEN_ACK   stream 1   {"offset":4096}        # server already has 4096 bytes

# The agent seeks to offset 4096 and streams data without waiting:
A -> S   DATA       stream 1   offset=4096  body=…
A -> S   DATA       stream 1   offset=69632 body=…
S -> A   ACK        stream 1   {"offset":69632}       # durably stored up to here
…

A -> S   HEARTBEAT  stream 0   {}                     # when otherwise idle
S -> A   HEARTBEAT  stream 0   {}

# Graceful shutdown:
A -> S   CLOSE      stream 0   {"reason":"shutting down"}
```

**Authentication.** The server verifies `auth.token` by comparing the
SHA-256 hash of the received token, in constant time, against its configured
set of allowed hashes. A failed handshake is answered with `ERROR` and the
connection is closed. (TLS client certificates may be used as a stronger,
optional alternative.)

**Stream identity and rotation.** `OPEN` carries a prefix (the first bytes of
the file plus their SHA-256). The server keys the destination file on
`(hostname, path)` and uses the prefix to detect rotation: if the prefix no
longer matches the file it already has, it rotates that file aside and starts a
fresh one at offset 0. When the agent detects rotation locally (a new inode) it
`CLOSE`s the old stream and `OPEN`s a new one.

**Idempotent writes / resume.** `DATA.offset` is absolute. The server writes
only the part of the body beyond its current file length and drops anything it
already has; a frame whose offset is *beyond* the current length (a gap) is a
protocol error. Because of this, after any disconnect the agent can reconnect,
re-`OPEN`, and resume from the offset in `OPEN_ACK` with no duplicated or
missing bytes.

**Flow control.** The server sends a cumulative `ACK` for a stream at most
every `ack_interval` seconds (and on graceful close). The agent keeps the total
of unacknowledged `DATA` bytes below its configured window; when the window is
full it stops reading new data until an `ACK` advances the acknowledged offset.

**Heartbeats.** If either side has sent nothing for `heartbeat_interval`
seconds it sends a `HEARTBEAT`. If a peer receives nothing at all for
`idle_timeout` seconds it considers the connection dead and closes it.
