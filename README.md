Logline
=======

Live synchronization of log files from many machines (VMs, servers…) to a
single central place.

(A *log file* here is a regular file that only grows — new content is appended —
and may be rotated.)

It consists of two pieces:

- **agent** runs on each source machine, tails the configured log files and
  ships new content to the server over a single (optionally TLS-encrypted) TCP
  connection.
- **server** listens on a TCP port, receives data from many agents and keeps a
  mirror of every log file under a destination directory.

The agent is deliberately **standard-library only**, so it can run on an older
machine directly on the system Python without a virtualenv. The server runs in
Docker with its dependencies pinned via `uv`.


How it works
------------

The agent and server speak **logline/2**, a small framed binary protocol
([Protocol.md](Protocol.md)):

- **One connection per agent.** Every watched file is a *stream* multiplexed on
  the same connection, so there is a single handshake regardless of how many
  files are followed.
- **Pipelined and flow-controlled.** The agent streams data without waiting for
  a reply to each chunk; the server sends cumulative acknowledgements, and the
  agent keeps the amount of un-acknowledged data below a window.
- **Resumable, exactly mirrored.** Every chunk carries an absolute file offset.
  The server writes idempotently, so after a disconnect the agent just
  reconnects and resumes from the offset the server reports — no gaps, no
  duplicates on disk.
- **Rotation aware.** When a source file is rotated the server keeps the old
  mirror aside and starts a fresh one.
- **Compressed** (gzip/deflate, both stdlib) and optionally **TLS-encrypted**,
  using a certificate from e.g. Let's Encrypt or a self-signed one.


Running it
----------

Server (stores received logs under `./logs`, accepts one client token):

```
logline-server --bind :5645 --dest ./logs \
    --client-token-hash $(printf %s "$TOKEN" | sha256sum | cut -d' ' -f1)
```

Agent (tails matching files and ships them to the server):

```
CLIENT_TOKEN=$TOKEN logline-agent --server server.example.com:5645 \
    --scan '/var/log/*.log' --tls --tls-cert ca.pem
```

Both accept a YAML config file via `--conf`; see each component's
`configuration.py` for the supported keys, including a `tuning:` section for the
in-flight window, intervals, heartbeat, compression codec and `fsync` policy.


Development
-----------

Each component (`agent`, `server`, `e2e_tests`) is its own `uv` project. From
the repository root:

```
make check      # lint (ruff) + tests for every component
make image      # build the server Docker image
```
