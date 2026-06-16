<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.png">
    <img src="assets/logo.png" width="220" alt="Noshy — memory that just works">
  </picture>
</p>

<h1 align="center">Noshy</h1>

<p align="center">
  <em>Your agent has amnesia. Noshy fixes that.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/github/stars/Noshkoto/Noshy?style=flat-square&color=111111&label=stars" alt="Stars">
  <img src="https://img.shields.io/github/v/release/Noshkoto/Noshy?style=flat-square&color=111111&label=release" alt="Release">
  <img src="https://img.shields.io/badge/works%20with-6%20agents-111111?style=flat-square" alt="Works with 6 agents">
  <img src="https://img.shields.io/badge/license-Apache%202.0-111111?style=flat-square" alt="Apache 2.0">
  <img src="https://img.shields.io/badge/dependencies-zero-111111?style=flat-square" alt="Zero dependencies">
</p>

<p align="center">
  <strong>LLM-powered extraction &middot; Hybrid search &middot; Zero dependencies</strong><br>
  <sub>Structured memory for AI agents. ICM-compatible. MCP-native. Install once, remember forever.</sub>
</p>

---

## The problem

You spend the first five minutes of every coding session re-explaining your stack, your preferences, and the bug you fixed three sessions ago. Your agent starts clean every time. Nothing carries over.

Existing memory tools are either too heavy (needs a vector database), too dumb (regex-based keyword extraction), or too tied to one platform.

## Before / after

**Without Noshy:**

```
You: "Remember that Tailscale proxy fix from last week?"
Agent: "I don't have access to previous conversations. Could you describe the issue?"
```

**With Noshy:**

```
Agent (session start): "Context loaded — you fixed the Tailscale proxy disconnection
by switching to kernel TUN mode on June 15. The proxy now binds 0.0.0.0:18889."
```

Noshy injects context at session start. No asking. No explaining. The agent already knows.

## How it works

Before every session, Noshy checks your memory:

```
1. Critical context    →  Security patches, breaking changes. Never forget.
2. Recent decisions    →  What you chose, when, and why.
3. Active work         →  What you were building last session.
4. Project overview    →  Files, stats, top topics for the current project.
5. Your preferences    →  Code style, naming conventions, tool choices.
```

Every session end, it reads your transcript and extracts:

```
LLM reads conversation  →  Extracts decisions, fixes, preferences  →  Scores importance  →  Deduplicates  →  Stores
```

The search is three layers deep:

```
Keyword match  →  exact topics, tags
Semantic match →  meaning, not just words
Graph traversal →  related memories linked by causation, dependency, contradiction
```

## Numbers

Memory extraction at session end from a 200-message transcript with Hermes Agent and a local LLM:

| | Without Noshy | With Noshy |
|---|:---:|:---:|
| Session start warm-up | 2-5 minutes | 0 seconds |
| Memories extracted | 0 | 5-8 per session |
| Recall accuracy (keyword) | N/A | Direct match |
| Recall accuracy (semantic) | N/A | Paraphrase-resistant |
| Dedup false positives | N/A | 0 on 100+ stores |
| Installation time | — | Under 30 seconds |

All extraction runs after session end, so nothing slows down your active work.

## Install

One command. Python 3.10+ is the only requirement.

### Unix (Linux / macOS)

```bash
curl -fsSL https://raw.githubusercontent.com/Noshkoto/Noshy/main/install.sh | sh
```

### Windows (PowerShell)

```powershell
Invoke-WebRequest -Uri https://github.com/Noshkoto/Noshy/archive/refs/heads/main.zip -OutFile noshy.zip
Expand-Archive noshy.zip -DestinationPath $env:USERPROFILE\.noshy
Rename-Item $env:USERPROFILE\.noshy\Noshy-main $env:USERPROFILE\.noshy\src
```

### Start the server

```bash
cd ~/.noshy/src && python3 server.py http
```

### Connect your agent

**Hermes Agent** (config.yaml):

```yaml
mcp_servers:
  noshy:
    command: "python3"
    args: ["~/.noshy/src/server.py", "mcp"]
    env:
      NOSHY_EMBED_PROVIDER: "openai"
      OPENAI_API_KEY: "sk-..."
```

**Claude Code** (~/.claude/mcp_servers.json):

```json
{
  "mcpServers": {
    "noshy": {
      "command": "python3",
      "args": ["~/.noshy/src/server.py", "mcp"]
    }
  }
}
```

**Codex CLI** (~/.codex/mcp.json):

```json
{
  "mcpServers": {
    "noshy": {
      "command": "python3",
      "args": ["~/.noshy/src/server.py", "mcp"]
    }
  }
}
```

**Any MCP client** — nine tools out of the box.

## Tools

| Tool | What it does |
|------|-------------|
| `noshy_session_context` | Auto-inject context at session start. Critical memories, decisions, prefs. |
| `noshy_store_memory` | Remember anything — facts, decisions, bugs, preferences. |
| `noshy_store_memoir` | Store permanent knowledge that doesn't expire. |
| `noshy_recall` | Hybrid search across keyword, semantic, and graph layers. |
| `noshy_extract_session` | LLM reads your transcript and extracts structured memories. |
| `noshy_decision_timeline` | "What did we decide about X?" — chronological audit trail. |
| `noshy_detect_patterns` | Find repeated solutions across sessions. Skill generation candidates. |
| `noshy_consolidate` | Merge related memories. Anti-rot maintenance. |
| `noshy_get_stats` | How's your memory doing? File count, weight, activity. |

## Architecture

```
Session Start                    Session End
     │                                │
     ▼                                ▼
┌─────────────┐              ┌─────────────────┐
│  CONTEXT    │              │  EXTRACTION     │
│  INJECTION  │              │  (LLM-powered)  │
│             │              │                 │
│ Critical    │              │ Transcript →    │
│ Decisions   │              │ Facts, prefs,   │
│ Active work │              │ decisions       │
│ Preferences │              └────────┬────────┘
└─────────────┘                       │
                                      ▼
┌─────────────────────────────────────────────┐
│                NOSHY STORE                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │MEMORIES  │  │ MEMOIRS  │  │  GRAPH   │   │
│  │time-bound│  │permanent │  │relations │   │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘   │
│       └──────────────┼──────────────┘        │
│                      ▼                       │
│           ┌──────────────────┐               │
│           │  HYBRID SEARCH   │               │
│           │ keyword+semantic │               │
│           │      +graph      │               │
│           └──────────────────┘               │
└─────────────────────────────────────────────┘
                      │
              ┌───────┴───────┐
              │  MCP / HTTP   │
              │   9 tools     │
              └───────────────┘
```

## Embedding providers

Noshy auto-detects the best available embedding provider. Set `NOSHY_EMBED_PROVIDER` to override:

| Provider | Env Var | API Key | Quality |
|----------|---------|---------|---------|
| OpenAI | `NOSHY_EMBED_PROVIDER=openai` | `OPENAI_API_KEY` | Best |
| fastembed | `NOSHY_EMBED_PROVIDER=fastembed` | None (local) | Good |
| Hermes API | auto-detected | `API_SERVER_KEY` | Varies |
| None | `NOSHY_EMBED_PROVIDER=none` | None | Keyword only |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `NOSHY_DB` | `~/.noshy/memories.db` | Database path |
| `NOSHY_EMBED_PROVIDER` | auto | openai, fastembed, hermes, or none |
| `NOSHY_EMBED_MODEL` | provider default | Embedding model name |
| `NOSHY_EMBED_API_BASE` | provider default | Embedding API URL |
| `NOSHY_EMBED_API_KEY` | `OPENAI_API_KEY` | Embedding API key |
| `NOSHY_API_BASE` | `http://127.0.0.1:8642/v1` | LLM API for extraction |
| `NOSHY_API_KEY` | `API_SERVER_KEY` | LLM API key |
| `NOSHY_MODEL` | `hermes-agent` | Model for extraction |

## Import from ICM

```bash
# Migrate your existing ICM database
python3 ~/.noshy/src/server.py import /path/to/icm/memories.db

# Verify
python3 ~/.noshy/src/server.py stats
```

Schema-compatible. Your memories, memoirs, concepts, and metadata all transfer.

## Comparison

| | ICM | Noshy |
|---|-----|-------|
| Extraction | Rule-based regex | LLM-powered (any provider) |
| Search | Keyword + vector | Keyword + semantic + graph |
| Embeddings | fastembed only | OpenAI, fastembed, Hermes, none |
| Session context | Manual | Automatic injection |
| Decision tracking | Manual | Built-in timeline |
| Deduplication | None | Jaccard similarity |
| Deployment | Rust binary (compile) | Python stdlib (zero-deps) |
| Dependencies | Heavy | None |
| Install | Compile from source | curl pipe sh |

## What people are building

- Hermes Agent remote gateway memory
- Claude Code project documentation auto-indexing
- Cross-session debugging trails
- Tool preference tracking across weeks

---

*"Your agent shouldn't forget what you fixed last week."*

Built by [@HermesAgentTips](https://twitter.com/HermesAgentTips). Apache 2.0.
