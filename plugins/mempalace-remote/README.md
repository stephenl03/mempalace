# MemPalace Remote for Codex

This fork-maintained plugin connects Codex to the shared MemPalace HTTP MCP server and mirrors
the upstream local hook lifecycle without requiring a local MemPalace installation.

## Behavior

- `Stop` launches a detached checkpoint worker after each Codex turn. The worker saves after 15
  new human messages by default.
- `PreCompact` synchronously flushes any unsaved transcript messages before compaction.
- Each checkpoint writes an upstream-compatible `codex` diary entry and stores the new canonical
  Codex conversation turns verbatim in `wing_<project>/sessions` drawers.
- Checkpoint state advances only after every remote write succeeds. Remote failures never block
  Codex and are retried on a later lifecycle event.

The hook reads only canonical `event_msg` user and agent records. It ignores duplicate
`response_item` records and never writes the MCP bearer token, transcript contents, or HTTP headers
to its log.

## Authentication

Configure the remote server globally in `~/.codex/config.toml`. The static `Authorization` header
is read by both Codex and the lifecycle hook, so no token environment variable is required:

```toml
# BEGIN mempalace-remote managed MCP
[mcp_servers.mempalace]
url = "https://mempalace.k8s.lazy.sh/mcp"
http_headers = { Authorization = "Bearer replace-with-your-token" }
startup_timeout_sec = 20
tool_timeout_sec = 120
default_tools_approval_mode = "writes"
# END mempalace-remote managed MCP
```

The bundled configuration helper writes this block atomically, applies mode `0600`, reads the
token from standard input, and never prints it:

```bash
cd /path/to/mempalace
bw get password <item-id> | \
  python3 plugins/mempalace-remote/scripts/configure_static_token.py --token-stdin
```

This stores a plaintext bearer token in the Codex config. Protect the file and its backups, and do
not commit or share it. `MEMPALACE_MCP_TOKEN` remains supported as a compatibility fallback when
the static header is absent.

The configured endpoint is `https://mempalace.k8s.lazy.sh/mcp`. The hook uses the URL from the same
config block; `MEMPALACE_MCP_URL` can temporarily override it.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `MEMPAL_SAVE_INTERVAL` | `15` | New human messages between Stop checkpoints |
| `MEMPALACE_HOOKS_AUTO_SAVE` | enabled | Set to `0` or `false` to disable writes |
| `MEMPAL_DISABLE_HOOK` | disabled | Set to `1` to disable all hook work |
| `MEMPAL_REMOTE_MAX_DRAWER_CHARS` | `24000` | Maximum content per remote drawer call |
| `MEMPAL_REMOTE_TIMEOUT_SECONDS` | `20` | HTTP timeout per MCP tool call |
| `MEMPAL_REMOTE_CONFIG_FILE` | `~/.codex/config.toml` | Codex config containing static MCP auth |
| `MEMPAL_VERBOSE` | disabled | Show a UI message after successful foreground saves |

State and metadata-only logs live in Codex's `PLUGIN_DATA` directory. The fallback location is
`~/.codex/mempalace-remote/`.

## Privacy

The lifecycle hook sends new Codex user and assistant conversation text to the configured remote
MemPalace server verbatim. This intentionally follows MemPalace's verbatim-storage contract. Use
the hook only with a deployment you trust and control.
