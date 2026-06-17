![Noshy Architecture Diagram](https://i.ibb.co/qYTN01DC/Chat-GPT-Image-Jun-16-2026-01-38-43-PM.png)

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
# Install from PyPI (recommended)
pip install noshy

# Start the HTTP server + dashboard
noshy serve
# → http://127.0.0.1:8720/

# Or run as an MCP stdio server
noshy mcp
```

Or install from source:

```bash
curl -fsSL https://raw.githubusercontent.com/noshkoto/Noshy/main/install.sh | sh
```

## Usage

### CLI

```bash
# Store a memory (optional --ttl, --importance auto, --project)
python3 server.py store "deploy-config" "Deploy uses Cloudflare Pages with GitHub Actions"

# Recall (add --json for machine output)
python3 server.py recall "deployment config"

# List projects with counts and last activity
python3 server.py projects

# Delete: by id, by topic, or wipe an entire project
python3 server.py delete --id 01J...
python3 server.py delete --topic "old-bug" --scope onboarding
python3 server.py delete --project staging --yes

# Maintenance
python3 server.py purge                  # delete expired
python3 server.py consolidate-clusters   # merge near-duplicates
python3 server.py sweep                  # purge + decay + consolidate

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
| `noshy_store_memory` | Remember a fact, decision, or preference (optional `ttl_seconds` to auto-expire) |
| `noshy_store_memoir` | Store permanent knowledge (docs, reference) |
| `noshy_recall` | Search memories (keyword, semantic, hybrid) — also surfaces matching memoirs |
| `noshy_extract_session` | LLM-powered extraction from conversation transcripts |
| `noshy_stream_extract` | Incremental extraction for very long transcripts (chunked + overlap) |
| `noshy_consolidate` | Merge related memories on a topic |
| `noshy_delete` | Remove a memory by id, or all memories under a topic |
| `noshy_feedback` | Rate a memory +1/-1 to influence how long it survives |
| `noshy_list_projects` | List every project with per-project counts and last activity |
| `noshy_delete_project` | Wipe all memories and memoirs for a project (irreversible) |
| `noshy_predict_importance` | LLM-classify a candidate fact without storing it |
| `noshy_find_clusters` | Preview clusters of semantically near-duplicate memories |
| `noshy_consolidate_clusters` | Auto-merge those clusters in one pass |
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

# Recent memories (JSON)
curl 'http://127.0.0.1:8720/memories?limit=25'
```

### Web Dashboard

The HTTP server also serves a zero-dependency web dashboard. Start the server
and open the root URL in a browser:

```bash
python3 server.py http
# then visit http://127.0.0.1:8720/
```

Dashboard features:

- **Project picker** — filter recent memories and search by project
- **Hybrid search** — keyword + semantic + graph in one query box; memoirs included
- **Cluster view** — surface groups of near-duplicate memories and merge them in one click
- **Inline delete** — hover a card, click `×` to remove it (with confirmation)
- **Dark / light theme** — auto-detected, manually toggleable, persisted to `localStorage`

### Python API

For scripts and apps, Noshy ships a small Python API with decorators that
make any function self-remembering:

```python
import noshy

@noshy.remember(topic="deploy", importance="high")
def deploy(env):
    return f"deployed to {env}"

deploy("prod")                  # auto-stores: deploy -> 'deployed to prod'

# Scope memories to a project (and inherit tags) for a block of code
with noshy.session(project="checkout-bugfix", tags=["sprint-23"]):
    do_work()                   # every @remember inside picks up the project

noshy.recall("deploy")          # hybrid search returns matching memories
```

Useful keyword arguments on `@noshy.remember`:

- `importance="auto"` — let the LLM classify each memory (critical/high/medium/low)
- `on_error=True` (default) — exceptions are stored as high-importance memories
- `capture_args=True` — include arg names in the summary; arguments whose
  names look like secrets (`password`, `token`, `api_key`, …) are auto-redacted
- `skip_if=lambda r: r is None` — don't store certain return values
- `ttl_seconds=…` — auto-expire after N seconds

For long-running sessions, `noshy.extractor.stream_extract(chunks)` yields
memories incrementally as transcript chunks arrive.

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
# Build the image from the included Dockerfile
docker build -t noshy .

# Run it (data persists in a named volume)
docker run -d --name noshy \
  -p 8720:8720 \
  -v noshy-data:/data \
  -e OPENAI_API_KEY=sk-... \
  noshy

# Or with HTTP auth enabled
docker run -d --name noshy \
  -p 8720:8720 \
  -v noshy-data:/data \
  -e NOSHY_HTTP_TOKEN=$(openssl rand -hex 32) \
  noshy

# Optional build flags
docker build --build-arg WITH_FASTEMBED=1 -t noshy .   # bake local embeddings
docker build --build-arg WITH_SQLITE_VEC=0 -t noshy .  # skip the vec extension
```

The image runs as a non-root user, exposes a `/health` endpoint, and uses
`/data` as a persistent volume.

### HTTP authentication

By default the HTTP server is unauthenticated and binds to `127.0.0.1` only.
To expose it on a network or behind a proxy, set a bearer token:

```bash
export NOSHY_HTTP_TOKEN="$(openssl rand -hex 32)"
python3 server.py serve --host 0.0.0.0
```

Clients must then send `Authorization: Bearer <token>` on every request. The
`/health` endpoint and the dashboard HTML at `/` stay public so probes and
human visitors still work.

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
| `NOSHY_HTTP_TOKEN` | _unset_ | If set, all HTTP routes require `Authorization: Bearer <token>` (except `/health` and `/`) |

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

- [x] Web dashboard
- [x] Semantic search over memoirs (auto-embedded on store)
- [x] Automatic, importance-aware memory decay
- [x] Consolidation that prunes merged duplicates
- [x] Python decorator for automatic function memory (`@noshy.remember`)
- [x] Memory importance prediction (`importance="auto"`)
- [x] Streaming extraction (`extractor.stream_extract`)
- [x] Project isolation (`list_projects` / `delete_project`)
- [x] Graph-based memory consolidation (`find_clusters` / `consolidate_clusters`)
- [x] HTTP bearer-token auth (`NOSHY_HTTP_TOKEN`)
- [x] Real Dockerfile (multi-stage, non-root, healthcheck)
- [x] Integration test suite (`pytest tests/`)
- [x] PyPI release (`pip install noshy`)
- [x] Streaming extraction MCP tool (`noshy_stream_extract`)
- [x] Dashboard polish (project picker, cluster view, inline delete, theme toggle)
- [ ] Per-user database isolation (multi-tenant — one DB per token)

## License

Apache 2.0 — same as ICM. Built as a drop-in improvement.

---

*"Your agent shouldn't forget what you fixed last week."*
