# MemPalace Remote for Claude Code

`hooks/mempalace_remote_claude.py` is the Claude Code counterpart to the Codex
hook in `hooks/mempalace_remote.py`. It checkpoints Claude Code transcripts
straight to the shared MemPalace HTTP MCP server, so **Codex and Claude Code
write into one palace** — no local MemPalace install, no second local store to
drift out of sync.

## Behavior

| Event | Timeout | What happens |
|-------|---------|--------------|
| `SessionStart` | 5s | Creates the state dir. No writes. |
| `Stop` | 5s | Spawns a detached worker; saves after `MEMPAL_SAVE_INTERVAL` (15) new human messages. Returns immediately. |
| `PreCompact` | 30s | **Synchronous** flush of every unsaved turn before context is compacted. |
| `SessionEnd` | 10s | Detached final checkpoint so short sessions aren't lost. |

Each checkpoint stores the new canonical turns verbatim as
`wing_<project>/sessions` drawers (`added_by: claude-code`, keyed
`claude://<host>/<session>/<range>#chunk-N-of-M`) plus a `claude-code` diary
entry. Checkpoint state advances **only after every remote write succeeds**, so
a network failure is retried on the next lifecycle event instead of being lost.

## What gets stored

Only canonical `text` blocks from `user` / `assistant` entries. Deliberately
excluded:

- `thinking` blocks (internal reasoning),
- `tool_use` / `tool_result` blocks (duplicative and huge),
- `isSidechain` entries (subagent transcripts),
- `isMeta` entries and `<command-message>` payloads,
- `<system-reminder>` blocks, stripped inline — harness injection, not user speech.

## Configuration

Connection settings are read from `~/.claude.json` (`mcpServers.mempalace`),
the same block `claude mcp add` writes:

```jsonc
{ "mcpServers": { "mempalace": {
  "type": "http",
  "url": "https://mempalace.k8s.lazy.sh/mcp",
  "headers": { "Authorization": "Bearer <token>" }
} } }
```

The token is never logged, never passed on a command line, and never written to
`hook.log`. As with the Codex plugin, it is stored in plaintext in that config
file — protect it and its backups.

| Variable | Default | Purpose |
|---|---:|---|
| `MEMPAL_SAVE_INTERVAL` | `15` | New human messages between `Stop` checkpoints |
| `MEMPALACE_HOOKS_AUTO_SAVE` | enabled | `0`/`false` disables writes |
| `MEMPAL_DISABLE_HOOK` | disabled | `1` disables all hook work |
| `MEMPAL_REMOTE_MAX_DRAWER_CHARS` | `24000` | Max content per drawer call |
| `MEMPAL_REMOTE_TIMEOUT_SECONDS` | `20` | HTTP timeout per MCP call |
| `MEMPAL_REMOTE_FOREGROUND` | off | Run `Stop`/`SessionEnd` inline (testing) |
| `MEMPAL_VERBOSE` | off | Emit a `systemMessage` on successful save |
| `MEMPAL_REMOTE_DATA_DIR` | `~/.mempalace/claude-remote` | State + `hook.log` |
| `MEMPAL_REMOTE_TRANSCRIPT_ROOTS` | `~/.claude/projects` | Allowed transcript roots |
| `MEMPALACE_MCP_URL` / `MEMPALACE_MCP_TOKEN` | — | Override the config block |

## Install

Add to `~/.claude/settings.json` (user scope) or `.claude/settings.local.json`
(per project), alongside any hooks you already run:

```json
{
  "hooks": {
    "SessionStart": [{ "matcher": "*", "hooks": [{ "type": "command",
      "command": "python3 \"/abs/path/plugins/mempalace-remote/hooks/mempalace_remote_claude.py\" session-start",
      "timeout": 5 }] }],
    "Stop": [{ "hooks": [{ "type": "command",
      "command": "python3 \"…/mempalace_remote_claude.py\" stop",
      "timeout": 5 }] }],
    "PreCompact": [{ "matcher": "*", "hooks": [{ "type": "command",
      "command": "python3 \"…/mempalace_remote_claude.py\" precompact",
      "timeout": 30 }] }],
    "SessionEnd": [{ "hooks": [{ "type": "command",
      "command": "python3 \"…/mempalace_remote_claude.py\" session-end",
      "timeout": 10 }] }]
  }
}
```

Hooks fail open: any error is logged to `hook.log` and the hook still exits `0`,
so a palace outage never blocks Claude Code.

## Verify

```bash
TMP=$(mktemp -d)
printf '%s\n' '{"type":"user","cwd":"/tmp/x","timestamp":"2026-01-01T00:00:00Z","message":{"role":"user","content":"PROBE"}}' > "$TMP/t.jsonl"
MEMPAL_REMOTE_TRANSCRIPT_ROOTS="$TMP" MEMPAL_REMOTE_DATA_DIR="$TMP/state" \
MEMPAL_REMOTE_FOREGROUND=1 MEMPAL_VERBOSE=1 \
  echo "{\"session_id\":\"probe\",\"transcript_path\":\"$TMP/t.jsonl\",\"cwd\":\"/tmp/x\"}" \
  | python3 hooks/mempalace_remote_claude.py precompact
```

Expect `{"systemMessage":"MemPalace remote checkpoint saved"}`, then search the
palace for `PROBE`. Clean up test drawers with `mempalace_delete_by_source`
(pass the full `claude://…` source and `dry_run: false`).
