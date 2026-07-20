Logline
=======

Live synchronization of log files from multiple computers (usually VMs, servers…) to a single place.

(Log file = a regular file that only grows (new content is only appended) and can be rotated.)

Consists of two pieces:

- **agent** reads the logs and sends their content to the server via (encrypted) TCP connection
- **server** listens on a TCP port, receives data from agents and manages a mirror of the log files processed by agents

The TCP connection can be encrypted via SSL/TLS – using a certificate for example from LetsEncrypt.org or a self-signed one.

Authentication
--------------

Agents authenticate to the server with a shared secret client token. Logline
does not generate this token for you – pick your own random string, for
example with `openssl rand -hex 32`.

Configure it on the **agent** via one of (checked in this order):

- `--token-file <path>` command-line option
- `CLIENT_TOKEN` environment variable
- `client_token` in the agent's config file
- `client_token_file` in the agent's config file (path relative to the config file)

The **server** never stores the raw token, only its SHA-1 hex digest. Compute
it with:

```
echo -n 'your-secret-token' | sha1sum
```

and configure it via `client_token_hashes` (a list) in the server's config
file, or repeated `--client-token-hash` options. Multiple hashes can be
configured at once, so tokens can be issued per agent and rotated or revoked
individually. If a connecting agent's token doesn't match, the server logs
the hash it computed for the rejected token, which is convenient when
provisioning a new agent.

Scaling the server
------------------

A single server instance is single-threaded. To use more CPU cores you can run
multiple instances listening on the same address by passing the `--reuse-port`
option (which sets the `SO_REUSEPORT` socket option, available on Linux). The
kernel then load-balances incoming connections across all instances. In Docker,
run the instances with `--network=host` so they share the host's network stack.

On Linux all sockets sharing a port via `SO_REUSEPORT` must belong to the same
effective user, which prevents other users from hijacking the port. Be aware,
though, that any process running as the same user on the host can join the
listening group.
