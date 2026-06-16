# Noshy — Persistent Memory for AI Agents

**ICM-compatible. MCP-native. Works with any LLM.**

Noshy gives your AI agent real memory — not note-taking, not context stuffing, not a vector database you have to manage. Store facts, search across sessions, build knowledge graphs. It's what ICM wanted to be, re-built to work everywhere.

```
                     Noshy
          ┌───────────┼───────────┐
          │   MEMORIES            │   MEMOIRS
          │   (time-bound)        │   (permanent)
          │                       │
          │  ┌───┐ ┌───┐ ┌───┐    │   ┌───┐
          │  │bug│ │fix│ │pref│    │   │doc│
          │  └───┘ └───┘ └───┘    │   └───┘
          │    │       │     │     │
          │    └───┬───┘     │     │
          │  ┌─────┴─────┐   │     │
          │  │   GRAPH   │   │     │
          │  │  relations │   │     │
          │  └───────────┘   │     │
          └──────────────────┴─────┘
                    │
          ┌─────────┴──────────┐
          │   HYBRID SEARCH    │
          │  keyword+semantic  │
          │       +graph       │
          └────────────────────┘
```

## Why Noshy

- **LLM-powered extraction** — not regex. Uses any OpenAI-compatible API to extract structured facts from transcripts
- **Hybrid search** — keyword + semantic + graph recall in one query
- **ICM compatible** — import your existing ICM databases, uses the same schema
- **MCP native** — works with Claude Code, Hermes, Codex, Copilot, and any MCP client
- **Any embedding provider** — OpenAI, fastembed (local, free), or Hermes API server
- **Zero dependencies** — core runs on Python stdlib. fastembed and OpenAI are optional
- **Single binary feel** — one Python file does everything

## Quick Start

```bash
# Install
curl -fsSL https://raw.githubusercontent.com/noshkoto/Noshy/main/install.sh | sh

# Start HTTP server
cd ~/.noshy/src && python3 server.py http

# Or MCP stdio mode
cd ~/.noshy/src && python3 server.py mcp
```

## Usage

### CLI

```bash
# Store a memory
python3 server.py store "deploy-config" "Deploy uses Cloudflare Pages with GitHub Actions"

# Recall
python3 server.py recall "deployment config"

# Import from ICM
python3 server.py import /path/to/icm/memories.db

# Stats
python3 server.py stats
```

### MCP Server (Claude Code, Hermes, Codex, Copilot)

Add to your MCP client config:

**Claude Code** (`~/.claude/mcp_servers.json`):
```json
{
  "mcpServers": {
    "noshy": {
      "command": "python3",
      "args": ["/path/to/noshy/server.py", "mcp"],
      "env": {
        "NOSHY_EMBED_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-..."
      }
    }
  }
}
```

**Hermes** (`config.yaml`):
```yaml
mcp_servers:
  noshy:
    command: "python3"
    args: ["/path/to/noshy/server.py", "mcp"]
    env:
      NOSHY_EMBED_PROVIDER: "openai"
      OPENAI_API_KEY: "sk-..."
```

**Codex CLI** (`~/.codex/mcp.json`):
```json
{
  "mcpServers": {
    "noshy": {
      "command": "python3",
      "args": ["/path/to/noshy/server.py", "mcp"]
    }
  }
}
```

### MCP Tools

| Tool | What it does |
|------|-------------|
| `noshy_store_memory` | Remember a fact, decision, or preference |
| `noshy_store_memoir` | Store permanent knowledge (docs, reference) |
| `noshy_recall` | Search memories (keyword, semantic, hybrid) |
| `noshy_extract_session` | LLM-powered extraction from conversation transcripts |
| `noshy_consolidate` | Merge related memories on a topic |
| `noshy_get_stats` | Database overview |

### HTTP API

```bash
# Store
curl -X POST http://127.0.0.1:8720/tools/call \
  -H 'Content-Type: application/json' \
  -d '{"name":"noshy_store_memory","arguments":{"topic":"my-topic","summary":"What to remember"}}'

# Recall
curl -X POST http://127.0.0.1:8720/tools/call \
  -H 'Content-Type: application/json' \
  -d '{"name":"noshy_recall","arguments":{"query":"search keywords"}}'

# Stats
curl http://127.0.0.1:8720/stats
```

## Embedding Providers

Noshy auto-detects the best available embedding provider. Set `NOSHY_EMBED_PROVIDER` to override:

| Provider | Env Var | API Key | Quality |
|----------|---------|---------|---------|
| OpenAI | `NOSHY_EMBED_PROVIDER=openai` | `OPENAI_API_KEY` | Best |
| fastembed | `NOSHY_EMBED_PROVIDER=fastembed` | None (local) | Good |
| Hermes API | auto-detected | `API_SERVER_KEY` | Varies |
| None | No embedding | None | Keyword only |

```bash
# With OpenAI
export OPENAI_API_KEY="sk-..."
python3 server.py http

# With free local embeddings
pip install fastembed
python3 server.py http

# Keyword-only (no embeddings)
NOSHY_EMBED_PROVIDER=none python3 server.py http
```

## Platform Setup

### macOS
```bash
# Install Python 3.10+ if needed
brew install python@3.12

# Install Noshy
curl -fsSL https://raw.githubusercontent.com/noshkoto/Noshy/main/install.sh | sh

# Optional: local embeddings
pip3 install fastembed
```

### Linux
```bash
sudo apt install python3   # Debian/Ubuntu
sudo dnf install python3    # Fedora

curl -fsSL https://raw.githubusercontent.com/noshkoto/Noshy/main/install.sh | sh
```

### Windows
```powershell
# Install Python from python.org (check "Add to PATH")

# Download Noshy
Invoke-WebRequest -Uri https://github.com/noshkoto/Noshy/archive/refs/heads/main.zip -OutFile noshy.zip
Expand-Archive noshy.zip -DestinationPath $env:USERPROFILE\.noshy
Rename-Item $env:USERPROFILE\.noshy\Noshy-main $env:USERPROFILE\.noshy\src

# Run
python $env:USERPROFILE\.noshy\src\server.py http
```

### Docker
```bash
docker run -d --name noshy \
  -p 8720:8720 \
  -v noshy-data:/root/.noshy \
  -e OPENAI_API_KEY=sk-... \
  ghcr.io/noshkoto/Noshy:latest
```

## Configuration

| Env Variable | Default | Description |
|-------------|---------|-------------|
| `NOSHY_DB` | `~/.noshy/memories.db` | Database path |
| `NOSHY_EMBED_PROVIDER` | auto | openai, fastembed, hermes, or none |
| `NOSHY_EMBED_MODEL` | provider default | Embedding model name |
| `NOSHY_EMBED_API_BASE` | provider default | Embedding API URL |
| `NOSHY_EMBED_API_KEY` | `OPENAI_API_KEY` | Embedding API key |
| `NOSHY_API_BASE` | `http://127.0.0.1:8642/v1` | LLM API for extraction |
| `NOSHY_API_KEY` | `API_SERVER_KEY` | LLM API key |
| `NOSHY_MODEL` | `hermes-agent` | Model for extraction |

## Architecture

```
┌─────────────────────────────────────────┐
│              Noshy MCP Server            │
│  ┌──────────┐ ┌────────┐ ┌───────────┐  │
│  │Extractor │ │ Store  │ │  Embedder │  │
│  │(LLM API) │ │(SQLite)│ │(OpenAI/   │  │
│  │          │ │        │ │ fastembed) │  │
│  └──────────┘ └────────┘ └───────────┘  │
│         │          │           │         │
│         └──────────┼───────────┘         │
│                    │                     │
│        ┌───────────┴────────┐            │
│        │   Hybrid Search    │            │
│        │ keyword semantic   │            │
│        │      + graph       │            │
│        └────────────────────┘            │
│                    │                     │
│           ┌────────┴───────┐             │
│           │  MCP / HTTP    │             │
│           │  (stdio+API)   │             │
│           └────────────────┘             │
└─────────────────────────────────────────┘
```

## Import from ICM

```bash
# Import memories from an existing ICM database
python3 server.py import ~/.config/icm/memories.db

# Verify
python3 server.py stats
```

The schema is compatible — memories, memoirs, concepts, and metadata all transfer. Graph edges and feedback are preserved when available.

## Comparison

| | ICM | Noshy |
|---|-----|------|
| Extraction | Rule-based regex | LLM-powered (any provider) |
| Search | Keyword + vector | Keyword + semantic + graph |
| Embeddings | fastembed only | OpenAI, fastembed, Hermes, none |
| Relationships | Memoir categories only | Full graph with weighted edges |
| Consolidation | Manual | LLM-assisted auto-merge |
| Deployment | Rust binary (compile) | Python stdlib (zero-deps core) |
| MCP | Yes | Yes |
| API | MCP only | MCP + HTTP + Python import |
| ICM import | N/A | Built-in |

## Roadmap

- [ ] Graph-based memory consolidation (auto-detect clusters)
- [ ] Memory importance prediction (LLM scoring)
- [ ] Streaming extraction (process transcripts as they arrive)
- [ ] Multi-user / project isolation
- [ ] Web dashboard
- [ ] Python decorator for automatic function memory

## License

Apache 2.0 — same as ICM. Built as a drop-in improvement.

---

*"Your agent shouldn't forget what you fixed last week."*
