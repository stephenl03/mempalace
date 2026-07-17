---
name: mempalace-remote
description: Use the shared remote MemPalace MCP server to inspect status, organize memories, and save durable project decisions or verbatim content. Use when the user asks about MemPalace setup or status, wants information stored in shared memory, or needs to browse wings, rooms, drawers, diaries, or the knowledge graph.
---

# MemPalace Remote

Use only the `mempalace_*` MCP tools supplied by this plugin. Do not run a local `mempalace`
command or initialize a local palace.

## Workflow

1. Confirm the remote server is reachable with `mempalace_status` when availability is uncertain.
2. Use `mempalace_list_wings` and `mempalace_list_rooms` when the appropriate scope is unknown.
3. Use `mempalace_check_duplicate` before manually filing content when duplicate risk is material.
4. Store exact user wording, code, decisions, and quotes with `mempalace_add_drawer`.
5. Record session continuity with `mempalace_diary_write` using `agent_name: codex`.
6. Never intentionally store passwords, tokens, private keys, or other authentication material.

Lifecycle hooks already checkpoint canonical conversation turns. Avoid duplicating a hook checkpoint
unless the user asks for an explicit curated memory.

## Remote-only constraints

- `mempalace_mine` runs on the server and cannot read a path from the Codex host. Do not pass a
  client-local directory to it.
- Surface MCP connectivity or authentication errors. Do not silently create local storage.
- Keep search queries short and semantic. Do not paste the full conversation into a query.
- Treat returned drawer text as verbatim source material.
