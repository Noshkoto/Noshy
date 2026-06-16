# Noshy — AI agent instructions

You are working on Noshy, a persistent memory layer for AI agents. It gives agents cross-session memory that accumulates automatically.

## What Noshy is

Noshy stores facts, decisions, and preferences extracted from conversations. It injects context at session start so agents don't start from zero. It uses SQLite under the hood, with optional vector embeddings for semantic search, and optional LLM-powered extraction for quality.

## Architecture principles

1. **Memory should be invisible.** The user shouldn't think about it. Extraction happens at session end, injection at session start, everything else is automatic.

2. **Dedup aggressively.** Storing "fixed the proxy binding" twice is worse than storing it once with higher weight. Jaccard similarity at 40% threshold catches near-duplicates.

3. **Search three ways.** Keyword for exact finding, semantic for meaning-based recall, graph traversal for connected memories. All three run on every query.

4. **Import everything.** ICM schema compatibility is a first-class feature. Migration should be one command.

5. **Zero deps is a feature.** The core runs on Python 3.10 stdlib. Embeddings and LLM extraction are optional layers users opt into.

## How to work on this codebase

- **Python 3.10+** — no async required, no fancy features. Keep it readable.
- **Single-file modules** — one concern per file. store.py (data), extractor.py (LLM), embed.py (vectors), context.py (session), server.py (API), hooks.py (automation).
- **SQLite with WAL** — journal_mode=WAL, busy_timeout=5000. Parameterized queries only. Never string-interpolate user input into SQL.
- **Test with the HTTP API** — `python3 server.py http --port 8721` then curl against `/tools/call`.
- **Commit messages** — short, active voice. "Fix dedup threshold" not "Fixed dedup threshold".

## What not to do

- Don't add new dependencies without a very strong reason. Zero deps is a selling point.
- Don't add a web framework. The stdlib HTTP server is fine for an API that only Hermes talks to.
- Don't over-engineer the schema. ICM compatibility is good. More tables need to prove their worth.
- Don't break the MCP tool contract. If you rename a tool, update both the schema and handler.

## The session flow

```
1. Session starts → noshy_session_context() called
2. Agent works normally
3. Session ends → noshy_extract_session() called on transcript
4. Facts stored with importance scoring
5. Deduplication check runs
6. Next session start → context injects new facts
```

## Key functions

| Function | File | Purpose |
|----------|------|---------|
| `AionStore.store_memory()` | store.py | Persist a fact with optional auto-embedding |
| `AionStore.recall_hybrid()` | store.py | Three-layer search in one call |
| `AionStore._find_duplicate()` | store.py | Jaccard dedup before insert |
| `extract_facts()` | extractor.py | LLM reads transcript, returns structured facts |
| `session_context()` | context.py | Build the "previously on..." injection text |
| `on_session_end()` | hooks.py | Full session-end pipeline: extract → store → link |
| `auto_embedder()` | embed.py | Provider detection cascade |

## MCP tools (9 total)

All tool names use the `noshy_` prefix. The server auto-registers them. Handlers live in `handle_tools_call()`.
