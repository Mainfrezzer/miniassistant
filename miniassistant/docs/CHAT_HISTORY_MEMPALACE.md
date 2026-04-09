# Chat History — Mempalace (Semantic Memory)

Your memory is powered by **mempalace** — a semantic search system.
You have the `search_memory` tool available.

## How to find past conversations

**Always use `search_memory` — NEVER `exec` with `grep`, `find`, or `cat` on memory files.**

```
search_memory(query="XFX Radeon GPU")
search_memory(query="VPN setup", n_results=10)
search_memory(query="Stable Diffusion", room="conversations")
```

## Rules

- When the user asks about **any** past conversation → call `search_memory` FIRST
- If no results → say so honestly. Do NOT fall back to grep/find/cat
- Results include similarity scores — higher = more relevant
- For broad searches, omit `wing` and `room`
- For specific topics, add `room` (e.g. `"conversations"`, `"technical"`)

## Trigger phrases — always use `search_memory`

- "erinnerst du dich an..." / "do you remember..."
- "was haben wir über X gesprochen?" / "did we talk about..."
- "such mal in deinem Gedächtnis" / "search your memory"
- "schau in der früheren Sitzung nach" / "check earlier session"
- "hatten wir das nicht mal besprochen?" / "didn't we discuss this?"

## What NOT to do

- NEVER use `exec` to grep/cat/find in memory files — `search_memory` replaces all of that
- NEVER guess or hallucinate past conversations
- NEVER use `read_url` with `file://` paths to read memory files
