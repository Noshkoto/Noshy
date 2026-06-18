"""
Noshy MCP server — exposes memory operations via MCP protocol and HTTP API.
Compatible with Hermes Agent, Claude Code, and any MCP client.
"""
import os
import sys
import json
import time
import logging
import argparse
from pathlib import Path
from typing import Optional, List, Dict, Any

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent))

from store import NoshyStore, _utcnow_iso as _now_iso
from extractor import extract_facts, consolidate_memories
from context import session_context, decision_timeline, detect_patterns, extract_preferences

def _build_log_handlers():
    """stderr always, plus a 5MB-rotating file handler when NOSHY_LOG_FILE is set
    (or ~/.noshy/noshy.log on Linux/macOS when stderr isn't a tty). Lets long-
    running containers log without filling disk."""
    handlers = [logging.StreamHandler(sys.stderr)]
    log_path = os.environ.get("NOSHY_LOG_FILE")
    if not log_path and not sys.stderr.isatty():
        # Headless run (Docker, systemd) — keep a rotating audit log on disk.
        log_path = str(Path.home() / ".noshy" / "noshy.log")
    if log_path:
        try:
            from logging.handlers import RotatingFileHandler
            p = Path(log_path).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(RotatingFileHandler(
                p, maxBytes=5_000_000, backupCount=3, encoding="utf-8"))
        except Exception:
            pass  # never let logging setup crash the server
    return handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_build_log_handlers(),
)
log = logging.getLogger("aion.server")

store: NoshyStore = None


# ──────────── MCP Protocol Handlers ────────────

MCP_TOOLS = [
    {
        "name": "noshy_store_memory",
        "description": "Store a new episodic memory. Use this to remember facts, decisions, preferences, and experiences.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Short topic slug in kebab-case"},
                "summary": {"type": "string", "description": "One-sentence factual summary of the memory"},
                "raw_excerpt": {"type": "string", "description": "Optional verbatim quote from source"},
                "keywords": {"type": "array", "items": {"type": "string"}, "description": "Keywords for recall"},
                "importance": {"type": "string", "enum": ["critical", "high", "medium", "low", "auto"], "default": "medium", "description": "Use 'auto' to have the LLM classify it"},
                "project": {"type": "string", "default": "default"},
                "ttl_seconds": {"type": "integer", "description": "Optional: auto-expire this memory after N seconds"},
            },
            "required": ["topic", "summary"],
        },
    },
    {
        "name": "noshy_store_memoir",
        "description": "Store permanent knowledge — facts, documentation, reference material that doesn't expire.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Title of the knowledge entry"},
                "content": {"type": "string", "description": "Full content of the knowledge entry"},
                "project": {"type": "string", "default": "default"},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "noshy_recall",
        "description": "Search and recall memories using keyword, semantic, or hybrid search.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query — topic, keyword, or natural language question"},
                "mode": {"type": "string", "enum": ["keyword", "semantic", "hybrid"], "default": "hybrid"},
                "limit": {"type": "integer", "default": 15, "minimum": 1, "maximum": 50},
                "project": {"type": "string", "description": "Filter by project"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "noshy_extract_session",
        "description": "Extract memories from a conversation transcript using LLM analysis.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "transcript": {"type": "string", "description": "Conversation transcript to extract facts from"},
                "project": {"type": "string", "default": "default"},
            },
            "required": ["transcript"],
        },
    },
    {
        "name": "noshy_stream_extract",
        "description": "Extract memories from a LONG transcript incrementally. Use this when the transcript is much longer than what a single LLM call can process (e.g., a multi-hour session log). Splits the input into overlapping chunks, runs extraction on each, and stores results as they're produced. Reports per-chunk progress.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "transcript": {"type": "string", "description": "Full transcript text (can be very long)"},
                "project": {"type": "string", "default": "default"},
                "chunk_chars": {"type": "integer", "default": 4000, "description": "Approx characters per chunk"},
                "max_memories_per_chunk": {"type": "integer", "default": 4},
            },
            "required": ["transcript"],
        },
    },
    {
        "name": "noshy_queue_extraction",
        "description": "Queue a transcript for later LLM extraction without blocking. Returns the pending row id immediately. A periodic sweep (or noshy_process_queue) drains the queue. Use this when a Hermes session wants to hand off a long transcript without waiting on the extractor.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "transcript": {"type": "string", "description": "Raw conversation transcript to queue"},
                "session_id": {"type": "string", "description": "Optional session identifier"},
                "project": {"type": "string", "default": "default"},
            },
            "required": ["transcript"],
        },
    },
    {
        "name": "noshy_process_queue",
        "description": "Drain up to `limit` pending extractions through the LLM extractor, storing resulting memories. Marks each row 'done' on success or 'failed' on exception. Returns processed/stored/failed counts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10, "description": "Max queue rows to process per call"},
                "project": {"type": "string", "default": "default"},
            },
        },
    },
    {
        "name": "noshy_consolidate",
        "description": "Merge related memories on a topic into one consolidated entry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Topic to consolidate memories for"},
                "min_weight": {"type": "number", "default": 0.3},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "noshy_get_stats",
        "description": "Get memory store statistics.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "noshy_session_context",
        "description": "Generate context for a new session — critical memories, recent decisions, active work, and preferences. Call this at the start of every session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Filter to specific project"},
                "max_memories": {"type": "integer", "default": 10},
                "last_session": {"type": "string", "description": "ISO timestamp of last session end"},
                "user_name": {"type": "string", "description": "Your name for personalization"},
            },
        },
    },
    {
        "name": "noshy_decision_timeline",
        "description": "Show a chronological timeline of all decisions, fixes, and resolutions. Use to answer 'what did we decide about X?'",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Filter to specific project"},
                "days": {"type": "integer", "default": 30, "description": "Look back this many days"},
            },
        },
    },
    {
        "name": "noshy_detect_patterns",
        "description": "Find repeated solutions across sessions — candidates for creating reusable skills.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Filter to specific project"},
                "min_occurrences": {"type": "integer", "default": 3, "description": "Min times a pattern must appear"},
            },
        },
    },
    {
        "name": "noshy_delete",
        "description": "Delete a memory that is wrong or outdated. Provide either an exact memory id, or a topic (optionally scoped to a project) to remove all memories under it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Exact memory id to delete"},
                "topic": {"type": "string", "description": "Delete all memories with this topic"},
                "project": {"type": "string", "description": "Scope a topic delete to one project"},
            },
        },
    },
    {
        "name": "noshy_feedback",
        "description": "Mark a memory as helpful (+1) or unhelpful (-1). Positive feedback helps a memory survive decay; negative feedback lets it fade out sooner.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Memory id to rate"},
                "score": {"type": "integer", "enum": [-1, 1], "description": "1 for helpful, -1 for unhelpful"},
                "reason": {"type": "string", "description": "Optional note on why"},
            },
            "required": ["id", "score"],
        },
    },
    {
        "name": "noshy_list_projects",
        "description": "List every project that has memories or memoirs, with counts and last-activity timestamps. Useful for understanding what's in the store.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "noshy_delete_project",
        "description": "Delete ALL memories and memoirs for a project. Use only when you're sure — this cannot be undone.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project to wipe"},
            },
            "required": ["project"],
        },
    },
    {
        "name": "noshy_predict_importance",
        "description": "Ask the LLM to classify a memory's importance (critical/high/medium/low) without storing it. Useful when deciding whether to keep a candidate fact.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["summary"],
        },
    },
    {
        "name": "noshy_find_clusters",
        "description": "Find clusters of near-duplicate memories using embedding similarity. Returns cluster previews without modifying anything.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "threshold": {"type": "number", "default": 0.85, "description": "Cosine similarity threshold (0-1)"},
                "project": {"type": "string", "description": "Limit to a project"},
                "min_size": {"type": "integer", "default": 2},
            },
        },
    },
    {
        "name": "noshy_consolidate_clusters",
        "description": "Auto-detect clusters of similar memories and consolidate each one. Returns counts. Run periodically to keep the store tidy.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "threshold": {"type": "number", "default": 0.88},
                "project": {"type": "string"},
                "max_clusters": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "noshy_find_contradictions",
        "description": "Find pairs of memories that may assert conflicting facts (e.g. opposite preferences, replaced decisions). Returns confirmed pairs with confidence and explanation; persists each as a 'contradicts' edge so future recalls can warn about them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Limit to a project"},
                "max_llm_checks": {"type": "integer", "default": 30, "description": "Cap LLM disambiguation calls per run"},
            },
        },
    },
]


def handle_initialize(params: Dict) -> Dict:
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "noshy", "version": "0.4.0"},
    }


def handle_tools_list(params: Dict = None) -> Dict:
    return {"tools": MCP_TOOLS}


def _split_transcript(text: str, target: int = 4000) -> List[str]:
    """Split a long transcript into roughly target-sized chunks, preferring
    paragraph boundaries so each chunk is self-contained.
    """
    if len(text) <= target:
        return [text]
    chunks: List[str] = []
    paragraphs = text.split("\n\n")
    buf: List[str] = []
    size = 0
    for p in paragraphs:
        plen = len(p) + 2  # include separator
        if size + plen > target and buf:
            chunks.append("\n\n".join(buf))
            buf, size = [p], plen
        else:
            buf.append(p)
            size += plen
    if buf:
        chunks.append("\n\n".join(buf))
    # Anything still oversized (one huge paragraph) — hard-slice
    out: List[str] = []
    for c in chunks:
        if len(c) <= target * 2:
            out.append(c)
        else:
            for i in range(0, len(c), target):
                out.append(c[i:i + target])
    return out


def handle_tools_call(params: Dict) -> Dict:
    name = params["name"]
    args = params.get("arguments", {})

    try:
        if name == "noshy_store_memory":
            mid = store.store_memory(
                topic=args["topic"],
                summary=args["summary"],
                raw_excerpt=args.get("raw_excerpt"),
                keywords=args.get("keywords"),
                importance=args.get("importance", "medium"),
                project=args.get("project", "default"),
                ttl_seconds=args.get("ttl_seconds"),
            )
            return {"content": [{"type": "text", "text": f"Memory stored: {mid} (topic: {args['topic']})"}]}

        elif name == "noshy_store_memoir":
            mid = store.store_memoir(
                title=args["title"],
                content=args["content"],
                project=args.get("project", "default"),
            )
            return {"content": [{"type": "text", "text": f"Memoir stored: {mid}"}]}

        elif name == "noshy_recall":
            mode = args.get("mode", "hybrid")
            query = args["query"]
            limit = args.get("limit", 15)
            project = args.get("project")

            if mode == "keyword":
                results = store.recall_by_topic(query, limit=limit, project=project)
            elif mode == "semantic":
                embedding = b""
                if store.embedder is not None:
                    try:
                        vecs = store.embedder.embed([query])
                        if vecs:
                            embedding = vecs[0]
                    except Exception as e:
                        log.debug(f"Query embed failed: {e}")
                results = store.recall_semantic(embedding, limit=limit, project=project)
            else:
                results = store.recall_hybrid(query, limit=limit, project=project)

            if not results:
                return {"content": [{"type": "text", "text": "No memories found."}]}

            # Batch-fetch contradicts edges for every returned memory id so we
            # can inline a "⚠ conflicts with: …" line without N+1 queries.
            ids = [r.get("id") for r in results if r.get("id") and r.get("_kind") != "memoir"]
            conflicts = store.get_contradictions_for(ids) if ids else {}

            def _fmt(r):
                if r.get("_kind") == "memoir":
                    return f"[MEMOIR] {r.get('topic', 'memoir')}\n{r.get('summary', '')}"
                imp = (r.get("importance") or "medium").upper()
                head = f"[{imp}] {r.get('topic', 'unknown')}\n{r.get('summary', '')}"
                other = conflicts.get(r.get("id")) if r.get("id") else None
                if other:
                    first = other[0]
                    snip = (first.get("summary") or "")[:80]
                    more = f" (+{len(other) - 1} more)" if len(other) > 1 else ""
                    head = (f"⚠ conflicts with: {first.get('topic', 'memory')} — "
                            f"\"{snip}\"{more}\n" + head)
                return head

            out = "\n\n".join(_fmt(r) for r in results)
            return {"content": [{"type": "text", "text": out}]}

        elif name == "noshy_extract_session":
            facts = extract_facts(transcript=args["transcript"])
            if not facts:
                return {"content": [{"type": "text", "text": "No facts extracted."}]}
            count = store.apply_extracted_facts(
                facts, project=args.get("project", "default"), source="extract")
            return {"content": [{"type": "text", "text": f"Extracted and stored {count} memories."}]}

        elif name == "noshy_stream_extract":
            from extractor import stream_extract
            transcript = args["transcript"]
            project = args.get("project", "default")
            chunk_chars = max(500, int(args.get("chunk_chars", 4000)))
            mpc = max(1, int(args.get("max_memories_per_chunk", 4)))

            # Split into roughly chunk_chars-sized pieces, preferring paragraph breaks
            chunks = _split_transcript(transcript, chunk_chars)
            total_stored = 0
            chunk_count = 0
            for facts in stream_extract(
                chunks,
                max_memories_per_chunk=mpc,
                chunk_overlap=min(400, chunk_chars // 4),
            ):
                chunk_count += 1
                total_stored += store.apply_extracted_facts(
                    facts, project=project, source="stream-extract")
            return {"content": [{"type": "text",
                "text": f"Streamed {len(chunks)} chunks, {chunk_count} produced facts, "
                        f"stored {total_stored} memories total."}]}

        elif name == "noshy_queue_extraction":
            pid = store.queue_extraction(
                raw_text=args["transcript"],
                session_id=args.get("session_id"),
                source="mcp-queue",
            )
            return {"content": [{"type": "text",
                "text": f"Queued extraction: {pid} (will be processed on the next sweep)."}]}

        elif name == "noshy_process_queue":
            counts = store.process_extraction_queue(
                limit=int(args.get("limit", 10)),
                project=args.get("project", "default"),
            )
            return {"content": [{"type": "text",
                "text": f"Queue: processed {counts['processed']}, "
                        f"stored {counts['stored']} memories, "
                        f"{counts['failed']} failed."}]}

        elif name == "noshy_consolidate":
            count = store.consolidate(
                topic=args["topic"],
                min_weight=args.get("min_weight", 0.3),
            )
            return {"content": [{"type": "text", "text": f"Consolidated {count} memories."}]}

        elif name == "noshy_get_stats":
            stats = store.get_stats()
            lines = [f"{k}: {v}" for k, v in stats.items()]
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        elif name == "noshy_session_context":
            ctx = session_context(
                project=args.get("project"),
                max_memories=args.get("max_memories", 10),
                last_session=args.get("last_session"),
                user_name=args.get("user_name"),
            )
            return {"content": [{"type": "text", "text": ctx}]}

        elif name == "noshy_decision_timeline":
            tl = decision_timeline(
                project=args.get("project"),
                days=args.get("days", 30),
            )
            return {"content": [{"type": "text", "text": tl}]}

        elif name == "noshy_detect_patterns":
            patterns = detect_patterns(
                project=args.get("project"),
                min_occurrences=args.get("min_occurrences", 3),
            )
            if not patterns:
                return {"content": [{"type": "text", "text": "No patterns detected yet."}]}
            out = "\n".join(
                f"{p['topic']} ({p['occurrences']}x): {p['suggested_action']}"
                for p in patterns
            )
            return {"content": [{"type": "text", "text": out}]}

        elif name == "noshy_delete":
            mem_id = args.get("id")
            topic = args.get("topic")
            if mem_id:
                ok = store.delete_memory(mem_id)
                msg = f"Deleted memory {mem_id}." if ok else f"No memory found with id {mem_id}."
            elif topic:
                n = store.delete_by_topic(topic, project=args.get("project"))
                msg = f"Deleted {n} memory(ies) under topic '{topic}'."
            else:
                return {"content": [{"type": "text", "text": "Provide either 'id' or 'topic' to delete."}], "isError": True}
            return {"content": [{"type": "text", "text": msg}]}

        elif name == "noshy_feedback":
            ok = store.record_feedback(args["id"], int(args["score"]), reason=args.get("reason"))
            if not ok:
                return {"content": [{"type": "text", "text": f"No memory found with id {args['id']}."}], "isError": True}
            verb = "boosted" if int(args["score"]) == 1 else "demoted"
            return {"content": [{"type": "text", "text": f"Feedback recorded — memory {verb}."}]}

        elif name == "noshy_list_projects":
            projects = store.list_projects()
            if not projects:
                return {"content": [{"type": "text", "text": "No projects yet."}]}
            lines = []
            for p in projects:
                last = (p.get("last_activity") or "")[:10]
                lines.append(
                    f"{p['project']}: {p['memory_count']} memories, "
                    f"{p['memoir_count']} memoirs (last: {last})"
                )
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        elif name == "noshy_delete_project":
            counts = store.delete_project(args["project"])
            return {"content": [{"type": "text",
                "text": f"Deleted project '{args['project']}': "
                        f"{counts['memories']} memories, {counts['memoirs']} memoirs."}]}

        elif name == "noshy_predict_importance":
            from extractor import predict_importance
            score = predict_importance(args.get("topic", ""), args["summary"])
            return {"content": [{"type": "text", "text": score}]}

        elif name == "noshy_find_clusters":
            clusters = store.find_clusters(
                threshold=float(args.get("threshold", 0.85)),
                project=args.get("project"),
                min_size=int(args.get("min_size", 2)),
            )
            if not clusters:
                return {"content": [{"type": "text", "text": "No clusters detected."}]}
            lines = []
            for i, cluster in enumerate(clusters[:10], 1):
                lines.append(f"Cluster {i} ({len(cluster)} memories):")
                for m in cluster[:4]:
                    lines.append(f"  - {m['topic']}: {(m['summary'] or '')[:120]}")
                if len(cluster) > 4:
                    lines.append(f"  …and {len(cluster) - 4} more")
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        elif name == "noshy_consolidate_clusters":
            counts = store.consolidate_clusters(
                threshold=float(args.get("threshold", 0.88)),
                project=args.get("project"),
                max_clusters=int(args.get("max_clusters", 20)),
            )
            return {"content": [{"type": "text",
                "text": f"Consolidated {counts['clusters']} clusters, removed {counts['merged']} duplicates."}]}

        elif name == "noshy_find_contradictions":
            project = args.get("project")
            max_llm = int(args.get("max_llm_checks", 30))
            results = store.find_contradictions(
                project=project, max_llm_checks=max_llm)
            if not results:
                return {"content": [{"type": "text", "text": "No contradictions detected."}]}
            # Persist confirmed pairs as `contradicts` edges (idempotent).
            edges = store._persist_contradiction_results(results)
            lines = [f"Found {len(results)} contradicting pair(s) "
                     f"({edges} new edge(s) recorded):"]
            for i, r in enumerate(results[:10], 1):
                a, b = r["memory_a"], r["memory_b"]
                conf = r["confidence"]
                lines.append(f"{i}. ({conf:.2f}) {a['topic']} ↔ {b['topic']}")
                lines.append(f"   A: {(a['summary'] or '')[:120]}")
                lines.append(f"   B: {(b['summary'] or '')[:120]}")
                if r.get("explanation"):
                    lines.append(f"   why: {r['explanation']}")
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        else:
            return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}

    except Exception as e:
        log.error(f"Tool error: {e}")
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}


# ──────────── MCP stdio mode ────────────

def run_stdio(db_path: str = None):
    """Run Noshy as an MCP stdio server."""
    global store
    from store_factory import get_store as _get_shared
    store = _get_shared(db_path=db_path)
    log.info(f"Noshy MCP stdio server ready (embed: {type(store.embedder).__name__})")

    def _send(payload: Dict):
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params", {}) or {}
        is_notification = req_id is None

        try:
            if method == "initialize":
                result = handle_initialize(params)
            elif method == "tools/list":
                result = handle_tools_list(params)
            elif method == "tools/call":
                result = handle_tools_call(params)
            elif method in ("notifications/initialized", "initialized"):
                continue
            elif method == "shutdown":
                if not is_notification:
                    _send({"jsonrpc": "2.0", "id": req_id, "result": {}})
                break
            else:
                if not is_notification:
                    _send({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Unknown method: {method}"},
                    })
                continue

            if not is_notification:
                _send({"jsonrpc": "2.0", "id": req_id, "result": result})

        except Exception as e:
            log.exception("MCP handler error")
            if not is_notification:
                _send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32603, "message": str(e)},
                })


# ──────────── Web Dashboard ────────────

def _dashboard_candidates() -> List[Path]:
    """Where to look for dashboard.html. First match wins.

    - dev / source checkout: sibling of this server.py
    - pip wheel install: <sys.prefix>/share/noshy/dashboard.html
      (matches [tool.setuptools.data-files] in pyproject.toml)
    - last-resort user override: ~/.noshy/dashboard.html
    """
    return [
        Path(__file__).parent / "dashboard.html",
        Path(sys.prefix) / "share" / "noshy" / "dashboard.html",
        Path.home() / ".noshy" / "dashboard.html",
    ]


def _load_dashboard_html() -> str:
    """Read and cache the dashboard HTML from disk on first use."""
    try:
        return _load_dashboard_html._cache
    except AttributeError:
        pass
    html = None
    for p in _dashboard_candidates():
        try:
            html = p.read_text(encoding="utf-8")
            break
        except (FileNotFoundError, OSError):
            continue
    if html is None:
        html = ("<!doctype html><meta charset=utf-8><title>Noshy</title>"
                "<p style=\"font-family:sans-serif;padding:2em\">"
                "dashboard.html is missing from this install — reinstall noshy "
                "or copy dashboard.html into ~/.noshy/ to override.</p>")
    _load_dashboard_html._cache = html
    return html

# Backwards-compat alias used by tests that read this at import time.
DASHBOARD_HTML = _load_dashboard_html()


# ──────────── HTTP API mode ────────────

def run_http(host: str = "127.0.0.1", port: int = 8720, db_path: str = None):
    """Run Noshy as an HTTP API server with graceful shutdown."""
    global store
    import hmac, signal
    from store_factory import get_store as _get_shared
    from config import get as _cfg
    store = _get_shared(db_path=db_path)

    # Token may come from env (NOSHY_HTTP_TOKEN) or ~/.noshy/config.toml
    auth_token = _cfg("http_token") or os.environ.get("NOSHY_HTTP_TOKEN", "")
    if auth_token:
        log.info("HTTP auth enabled (Bearer token required)")
    # The dashboard HTML itself is always public — it carries the token prompt UI.
    # Health is public so containers can liveness-probe without secrets.
    # All data routes (/stats, /memories, /projects, /clusters, /tools/*, DELETE)
    # require the Bearer token when auth is enabled.
    public_paths = {"/health", "/", "/dashboard"}

    def _is_authorized(handler) -> bool:
        if not auth_token:
            return True
        if handler.path.split("?", 1)[0] in public_paths:
            return True
        header = handler.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        provided = header[len("Bearer "):].strip()
        return hmac.compare_digest(provided, auth_token)

    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, payload: Dict):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _require_auth(self) -> bool:
            if _is_authorized(self):
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="noshy"')
            self.send_header("Content-Type", "application/json")
            data = b'{"error":"unauthorized"}'
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return False

        def do_POST(self):
            if not self._require_auth():
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except (ValueError, json.JSONDecodeError) as e:
                self._send_json(400, {"error": f"bad request: {e}"})
                return

            try:
                if self.path == "/tools/call":
                    result = handle_tools_call({
                        "name": body.get("name"),
                        "arguments": body.get("arguments", {}),
                    })
                    self._send_json(200, result)
                elif self.path == "/extract":
                    transcript = body.get("transcript", "")
                    facts = extract_facts(transcript)
                    self._send_json(200, {"memories": facts})
                elif self.path == "/import-icm":
                    path = body.get("path", "")
                    count = store.import_icm(path)
                    self._send_json(200, {"imported": count})
                else:
                    self._send_json(404, {"error": "unknown endpoint"})
            except Exception as e:
                log.exception("HTTP POST error")
                self._send_json(500, {"error": str(e)})

        def do_DELETE(self):
            if not self._require_auth():
                return
            try:
                from urllib.parse import urlparse
                path = urlparse(self.path).path
                if path.startswith("/memories/"):
                    mem_id = path[len("/memories/"):]
                    if not mem_id:
                        self._send_json(400, {"error": "missing memory id"})
                        return
                    ok = store.delete_memory(mem_id)
                    if ok:
                        self._send_json(200, {"deleted": mem_id})
                    else:
                        self._send_json(404, {"error": "not found", "id": mem_id})
                else:
                    self._send_json(404, {"error": "unknown endpoint"})
            except Exception as e:
                log.exception("HTTP DELETE error")
                self._send_json(500, {"error": str(e)})

        def _send_html(self, status: int, html: str):
            data = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(self.path)
                path = parsed.path
                qs = parse_qs(parsed.query)

                # /health and dashboard HTML are public; API routes require auth when configured
                if path not in public_paths and not self._require_auth():
                    return

                if path in ("/", "/dashboard"):
                    self._send_html(200, _load_dashboard_html())
                elif path == "/stats":
                    self._send_json(200, store.get_stats())
                elif path == "/memories":
                    limit = int(qs.get("limit", ["25"])[0])
                    limit = max(1, min(limit, 200))
                    page = int(qs.get("page", ["1"])[0])
                    offset = max(0, (page - 1) * limit)
                    project = qs.get("project", [None])[0]
                    query = qs.get("q", [""])[0].strip()
                    if query:
                        # Hybrid search via the store; trim heavy fields for the wire
                        results = store.recall_hybrid(query, limit=limit, project=project)
                        out = []
                        for r in results:
                            d = {k: v for k, v in r.items() if k != "embedding"}
                            # Normalize memoir vs memory shape for the client
                            if d.get("_kind") == "memoir":
                                d["importance"] = "memoir"
                            out.append(d)
                        self._send_json(200, {"memories": out})
                    else:
                        sql = ("SELECT id, created_at, topic, summary, importance, weight, "
                               "project, access_count FROM memories "
                               "WHERE (expires_at IS NULL OR expires_at > ?)")
                        params = [_now_iso()]
                        if project:
                            sql += " AND project = ?"
                            params.append(project)
                        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
                        params.append(limit)
                        params.append(offset)
                        rows = [dict(r) for r in store.conn.execute(sql, params).fetchall()]
                        self._send_json(200, {"memories": rows})
                elif path == "/clusters":
                    threshold = float(qs.get("threshold", ["0.85"])[0])
                    project = qs.get("project", [None])[0]
                    clusters = store.find_clusters(threshold=threshold, project=project)
                    self._send_json(200, {"clusters": clusters[:20]})
                elif path == "/projects":
                    self._send_json(200, {"projects": store.list_projects()})
                elif path == "/tools/list":
                    self._send_json(200, {"tools": MCP_TOOLS})
                elif path == "/health":
                    self._send_json(200, {"status": "ok"})
                else:
                    self._send_json(404, {"error": "unknown endpoint"})
            except Exception as e:
                log.exception("HTTP GET error")
                self._send_json(500, {"error": str(e)})

        def log_message(self, format, *args):
            try:
                log.info("HTTP %s", format % args)
            except Exception:
                log.info("HTTP %s", " ".join(str(a) for a in args))

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    log.info(f"Noshy HTTP API running on http://{host}:{port}")

    def _graceful_shutdown(signum=None, frame=None):
        log.info("Shutdown signal received — closing store and stopping server")
        server.shutdown()
        if store:
            store.shutdown()

    import threading as _threading
    if _threading.current_thread() is _threading.main_thread():
        signal.signal(signal.SIGTERM, _graceful_shutdown)
        signal.signal(signal.SIGINT, _graceful_shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _graceful_shutdown()
    finally:
        if store:
            try:
                store.shutdown()
            except Exception:
                pass


# ──────────── CLI ────────────

def main():
    from config import get as _cfg
    default_host = _cfg("http_host", "127.0.0.1")
    default_port = int(_cfg("http_port", 8720))
    parser = argparse.ArgumentParser(description="Noshy — MCP-native memory for AI agents")
    parser.add_argument("--db", help="Database path", default=None)
    sub = parser.add_subparsers(dest="command")

    # stdio mode (MCP)
    sub.add_parser("mcp", help="Run as MCP stdio server")

    # HTTP mode
    http_p = sub.add_parser("http", help="Run as HTTP API server")
    http_p.add_argument("--host", default=default_host)
    http_p.add_argument("--port", type=int, default=default_port)

    # Import
    imp = sub.add_parser("import", help="Import from ICM database")
    imp.add_argument("icm_path", help="Path to ICM memories.db")

    # Per-subcommand --json flag. Goes after the subcommand:
    #   noshy stats --json
    def _add_json(sp):
        sp.add_argument("--json", action="store_true",
                        help="Emit JSON output instead of human-readable text")
        return sp

    _add_json(sub.add_parser("stats", help="Show memory stats"))
    recall_p = _add_json(sub.add_parser("recall", help="Recall memories"))
    recall_p.add_argument("query")
    recall_p.add_argument("--project", default=None)
    recall_p.add_argument("--limit", type=int, default=15)

    store_p = _add_json(sub.add_parser("store", help="Store a memory"))
    store_p.add_argument("topic")
    store_p.add_argument("summary")
    store_p.add_argument("--importance", default="medium",
                        choices=["critical", "high", "medium", "low", "auto"])
    store_p.add_argument("--project", default="default")
    store_p.add_argument("--ttl", type=int, default=None,
                        help="Auto-expire after this many seconds")

    projects_p = _add_json(sub.add_parser("projects", help="List projects with counts and last activity"))

    del_p = _add_json(sub.add_parser("delete", help="Delete a memory by id, a topic, or a whole project"))
    del_g = del_p.add_mutually_exclusive_group(required=True)
    del_g.add_argument("--id", help="Exact memory id to delete")
    del_g.add_argument("--topic", help="Delete all memories under this topic")
    del_g.add_argument("--project", help="Delete an ENTIRE project (irreversible)")
    del_p.add_argument("--scope", help="Optional project scope for --topic")
    del_p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt for --project")

    cc_p = _add_json(sub.add_parser("consolidate-clusters",
                          help="Auto-detect and merge near-duplicate memories across topics"))
    cc_p.add_argument("--threshold", type=float, default=0.88)
    cc_p.add_argument("--project", default=None)
    cc_p.add_argument("--max-clusters", type=int, default=20)

    fc_p = _add_json(sub.add_parser("find-contradictions",
                          help="Detect (and link) pairs of memories that may assert conflicting facts"))
    fc_p.add_argument("--project", default=None)
    fc_p.add_argument("--max-llm-checks", type=int, default=30)

    q_p = _add_json(sub.add_parser("queue",
                          help="Queue a transcript for later extraction (does not block on LLM)"))
    q_g = q_p.add_mutually_exclusive_group(required=True)
    q_g.add_argument("transcript", nargs="?", help="Transcript text (positional)")
    q_g.add_argument("--file", help="Read transcript from a file path")
    q_p.add_argument("--session-id", default=None)

    pq_p = _add_json(sub.add_parser("process-queue",
                          help="Drain queued extractions through the LLM"))
    pq_p.add_argument("--limit", type=int, default=10)
    pq_p.add_argument("--project", default="default")

    _add_json(sub.add_parser("purge", help="Delete expired memories now"))
    _add_json(sub.add_parser("sweep", help="Run the full maintenance sweep (purge + decay + consolidate)"))

    # "serve" is a friendly alias for "http"
    serve_p = sub.add_parser("serve", help="Alias for `http` — start the HTTP server + dashboard")
    serve_p.add_argument("--host", default=default_host)
    serve_p.add_argument("--port", type=int, default=default_port)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    db = getattr(args, 'db', None)
    as_json = getattr(args, 'json', False)

    if args.command == "mcp":
        run_stdio(db_path=db)
        return
    if args.command in ("http", "serve"):
        run_http(args.host, args.port, db_path=db)
        return

    global store
    from store_factory import get_store as _get_shared
    store = _get_shared(db_path=db)

    def _out(text_lines, payload):
        if as_json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            print("\n".join(text_lines))

    if args.command == "import":
        count = store.import_icm(args.icm_path)
        _out([f"Imported {count} memories from {args.icm_path}"],
             {"imported": count, "source": args.icm_path})
    elif args.command == "stats":
        stats = store.get_stats()
        _out([f"{k}: {v}" for k, v in stats.items()], stats)
    elif args.command == "recall":
        results = store.recall_hybrid(args.query, limit=args.limit, project=args.project)
        if as_json:
            slim = [{k: v for k, v in r.items() if k != "embedding"} for r in results]
            print(json.dumps(slim, indent=2, default=str))
        elif not results:
            print("No memories found.")
        else:
            for i, r in enumerate(results, 1):
                imp = (r.get('importance') or 'medium').upper()
                kind = " [MEMOIR]" if r.get("_kind") == "memoir" else ""
                print(f"{i}. [{imp}]{kind} {r.get('topic') or r.get('title')}")
                print(f"   {(r.get('summary') or r.get('content') or '')[:240]}\n")
    elif args.command == "store":
        mid = store.store_memory(
            topic=args.topic, summary=args.summary,
            importance=args.importance, project=args.project,
            ttl_seconds=args.ttl,
        )
        _out([f"Stored: {mid}"], {"id": mid, "topic": args.topic, "project": args.project})
    elif args.command == "projects":
        projs = store.list_projects()
        if as_json:
            print(json.dumps(projs, indent=2, default=str))
        elif not projs:
            print("No projects yet.")
        else:
            for p in projs:
                last = (p.get("last_activity") or "")[:10]
                print(f"{p['project']:24} {p['memory_count']:>5} memories  "
                      f"{p['memoir_count']:>3} memoirs  (last: {last})")
    elif args.command == "delete":
        if args.id:
            ok = store.delete_memory(args.id)
            _out([f"{'Deleted' if ok else 'Not found:'} {args.id}"],
                 {"deleted": int(ok), "id": args.id})
        elif args.topic:
            n = store.delete_by_topic(args.topic, project=args.scope)
            _out([f"Deleted {n} memory(ies) under topic '{args.topic}'"],
                 {"deleted": n, "topic": args.topic, "scope": args.scope})
        elif args.project:
            if not args.yes:
                resp = input(f"Delete ALL memories and memoirs for project "
                             f"'{args.project}'? Type the project name to confirm: ")
                if resp.strip() != args.project:
                    print("Aborted.")
                    return
            counts = store.delete_project(args.project)
            _out([f"Deleted project '{args.project}': {counts['memories']} memories, "
                  f"{counts['memoirs']} memoirs"],
                 {"project": args.project, **counts})
    elif args.command == "consolidate-clusters":
        counts = store.consolidate_clusters(
            threshold=args.threshold, project=args.project,
            max_clusters=args.max_clusters,
        )
        _out([f"Consolidated {counts['clusters']} clusters, "
              f"removed {counts['merged']} duplicates"], counts)
    elif args.command == "find-contradictions":
        results = store.find_contradictions(
            project=args.project, max_llm_checks=args.max_llm_checks)
        edges = store._persist_contradiction_results(results)
        if as_json:
            print(json.dumps({"results": results, "new_edges": edges},
                             indent=2, default=str))
        elif not results:
            print("No contradictions detected.")
        else:
            print(f"Found {len(results)} contradicting pair(s) "
                  f"({edges} new edge(s) recorded):")
            for i, r in enumerate(results, 1):
                a, b = r["memory_a"], r["memory_b"]
                print(f"{i}. ({r['confidence']:.2f}) {a['topic']} ↔ {b['topic']}")
                print(f"   A: {(a['summary'] or '')[:200]}")
                print(f"   B: {(b['summary'] or '')[:200]}")
                if r.get("explanation"):
                    print(f"   why: {r['explanation']}")
    elif args.command == "queue":
        text = args.transcript
        if args.file:
            with open(args.file, "r", encoding="utf-8") as fh:
                text = fh.read()
        pid = store.queue_extraction(
            text, session_id=args.session_id, source="cli-queue")
        _out([f"Queued: {pid}"], {"id": pid})
    elif args.command == "process-queue":
        counts = store.process_extraction_queue(
            limit=args.limit, project=args.project)
        _out([f"Processed {counts['processed']}, stored {counts['stored']} "
              f"memories, {counts['failed']} failed"], counts)
    elif args.command == "purge":
        n = store.purge_expired()
        _out([f"Purged {n} expired memories"], {"purged": n})
    elif args.command == "sweep":
        from hooks import daily_sweep
        # daily_sweep instantiates its own store, but we already opened the DB;
        # it'll honor NOSHY_DB if set, so just call it.
        result = daily_sweep()
        q = result.get("queue", {})
        _out([f"Sweep: purged={result['purged']}, "
              f"consolidated={result['consolidated']}, "
              f"clusters={result.get('clusters', 0)}, "
              f"queue_processed={q.get('processed', 0)}, "
              f"queue_failed={q.get('failed', 0)}",
              f"Store: {result['stats']['memory_count']} memories, "
              f"avg weight {(result['stats']['avg_weight'] or 0):.2f}"],
             result)


if __name__ == "__main__":
    main()
