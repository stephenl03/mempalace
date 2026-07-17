---
name: mempalace-remote-recall
description: Search the shared remote MemPalace before answering about past work, prior decisions, people, projects, preferences, or earlier sessions. Use when the user asks what was decided, tried, discussed, or remembered, or when current work may depend on knowledge saved by another connected host.
---

# MemPalace Remote Recall

Use remote palace evidence instead of guessing from model memory.

## Protocol

1. Call `mempalace_search` before answering memory-relevant questions. Use a short natural-language
   query and avoid wing filters on the first search unless the scope is certain.
2. Use `mempalace_kg_query` or `mempalace_kg_timeline` for relational or time-bound facts.
3. Use `mempalace_diary_read` with `agent_name: codex` for recent session continuity.
4. Quote relevant drawer text verbatim. Clearly distinguish retrieved evidence from inference.
5. If results are empty, say so and offer to widen the search. Do not invent an answer.
6. If the remote server is unavailable, surface the error and do not fall back to a local palace.

## Tool selection

| Need | Tool |
|---|---|
| Semantic memory search | `mempalace_search` |
| Entity relationship or current fact | `mempalace_kg_query` |
| Entity history | `mempalace_kg_timeline` |
| Recent Codex continuity | `mempalace_diary_read` |
| Discover scope | `mempalace_list_wings`, then `mempalace_list_rooms` |

Skip remote recall for clearly greenfield work with no connection to prior context.
