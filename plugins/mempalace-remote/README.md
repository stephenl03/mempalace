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

Set `MEMPALACE_MCP_TOKEN` in the environment that launches Codex. Do not put the token in this
repository, `.mcp.json`, `config.toml`, or `hooks.json`.

The configured endpoint is `https://mempalace.k8s.lazy.sh/mcp`. Override it for another deployment
with `MEMPALACE_MCP_URL`.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `MEMPAL_SAVE_INTERVAL` | `15` | New human messages between Stop checkpoints |
| `MEMPALACE_HOOKS_AUTO_SAVE` | enabled | Set to `0` or `false` to disable writes |
| `MEMPAL_DISABLE_HOOK` | disabled | Set to `1` to disable all hook work |
| `MEMPAL_REMOTE_MAX_DRAWER_CHARS` | `24000` | Maximum content per remote drawer call |
| `MEMPAL_REMOTE_TIMEOUT_SECONDS` | `20` | HTTP timeout per MCP tool call |
| `MEMPAL_VERBOSE` | disabled | Show a UI message after successful foreground saves |

State and metadata-only logs live in Codex's `PLUGIN_DATA` directory. The fallback location is
`~/.codex/mempalace-remote/`.

## Privacy

The lifecycle hook sends new Codex user and assistant conversation text to the configured remote
MemPalace server verbatim. This intentionally follows MemPalace's verbatim-storage contract. Use
the hook only with a deployment you trust and control.
