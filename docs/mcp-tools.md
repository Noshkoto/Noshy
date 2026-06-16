# MCP Tool Reference

All nine Noshy MCP tools with full input schemas and examples.

## noshy_session_context

Generate context for a new session. Call this at session start.

```json
{
  "name": "noshy_session_context",
  "arguments": {
    "project": "hermes",
    "max_memories": 10
  }
}
```

Returns: Critical memories, recent decisions, active work, project overview, preferences.

## noshy_store_memory

Store a fact, decision, or preference.

```json
{
  "name": "noshy_store_memory",
  "arguments": {
    "topic": "proxy-fix",
    "summary": "Changed proxy to bind 0.0.0.0 to fix Tailscale disconnections",
    "importance": "high",
    "keywords": ["proxy", "tailscale", "networking"],
    "project": "hermes"
  }
}
```

Required: `topic`, `summary`. Optional: `raw_excerpt`, `keywords`, `importance`, `project`.

## noshy_store_memoir

Store permanent knowledge that doesn't expire.

```json
{
  "name": "noshy_store_memoir",
  "arguments": {
    "title": "Deployment Architecture",
    "content": "All services run via systemd user units with Restart=always...",
    "project": "infra"
  }
}
```

## noshy_recall

Hybrid search across keyword, semantic, and graph layers.

```json
{
  "name": "noshy_recall",
  "arguments": {
    "query": "proxy binding fix",
    "mode": "hybrid",
    "limit": 10,
    "project": "hermes"
  }
}
```

Mode: `keyword` (fast, exact), `semantic` (meaning-based), `hybrid` (both, default).

## noshy_extract_session

LLM reads transcript and extracts structured memories.

```json
{
  "name": "noshy_extract_session",
  "arguments": {
    "transcript": "[user]: Fixed the proxy... [assistant]: Applied...",
    "project": "hermes"
  }
}
```

## noshy_decision_timeline

Chronological audit of every decision.

```json
{
  "name": "noshy_decision_timeline",
  "arguments": {
    "project": "hermes",
    "days": 30
  }
}
```

## noshy_detect_patterns

Find repeated solutions across sessions.

```json
{
  "name": "noshy_detect_patterns",
  "arguments": {
    "project": "hermes",
    "min_occurrences": 3
  }
}
```

Returns patterns with suggested actions (document, create skill, refactor).

## noshy_consolidate

Merge related memories to prevent rot.

```json
{
  "name": "noshy_consolidate",
  "arguments": {
    "topic": "proxy-fix",
    "min_weight": 0.3
  }
}
```

## noshy_get_stats

Database overview.

```json
{
  "name": "noshy_get_stats",
  "arguments": {}
}
```

Returns: `memory_count`, `memoir_count`, `concept_count`, `edge_count`, `avg_weight`.
