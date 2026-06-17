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
from embed import auto_embedder
from context import session_context, decision_timeline, detect_patterns, extract_preferences

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
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
]


def handle_initialize(params: Dict) -> Dict:
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "noshy", "version": "0.2.1"},
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

            def _fmt(r):
                if r.get("_kind") == "memoir":
                    return f"[MEMOIR] {r.get('topic', 'memoir')}\n{r.get('summary', '')}"
                imp = (r.get("importance") or "medium").upper()
                return f"[{imp}] {r.get('topic', 'unknown')}\n{r.get('summary', '')}"

            out = "\n\n".join(_fmt(r) for r in results)
            return {"content": [{"type": "text", "text": out}]}

        elif name == "noshy_extract_session":
            facts = extract_facts(
                transcript=args["transcript"],
            )
            if not facts:
                return {"content": [{"type": "text", "text": "No facts extracted."}]}

            count = 0
            for f in facts:
                if f.get("_type") == "relationship":
                    store.link_memories(f["source_id"], f["target_id"], f.get("relation", "related"))
                elif f.get("_type") == "concept":
                    pass  # handled during recall
                else:
                    store.store_memory(
                        topic=f["topic"],
                        summary=f["summary"],
                        keywords=f.get("keywords"),
                        importance=f.get("importance", "medium"),
                        source="extract",
                        project=args.get("project", "default"),
                    )
                    count += 1

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
                for f in facts:
                    if f.get("_type") == "relationship":
                        store.link_memories(f["source_id"], f["target_id"],
                                            f.get("relation", "related"))
                    elif f.get("_type") == "concept":
                        pass
                    else:
                        store.store_memory(
                            topic=f["topic"], summary=f["summary"],
                            keywords=f.get("keywords"),
                            importance=f.get("importance", "medium"),
                            source="stream-extract",
                            project=project,
                        )
                        total_stored += 1
            return {"content": [{"type": "text",
                "text": f"Streamed {len(chunks)} chunks, {chunk_count} produced facts, "
                        f"stored {total_stored} memories total."}]}

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

        else:
            return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}

    except Exception as e:
        log.error(f"Tool error: {e}")
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}


# ──────────── MCP stdio mode ────────────

def run_stdio(db_path: str = None):
    """Run Noshy as an MCP stdio server."""
    global store
    embedder = auto_embedder()
    store = NoshyStore(db_path=db_path, embedder=embedder)
    log.info(f"Noshy MCP stdio server ready (embed: {type(embedder).__name__})")

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

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Noshy — Memory</title>
<style>
/* ── TOKENS ─────────────────────────────────────────── */
:root {
  --bg:#080b12; --surface:rgba(255,255,255,.038); --surface2:rgba(255,255,255,.07);
  --border:rgba(255,255,255,.08); --border-h:rgba(99,102,241,.55);
  --text:#e2e4f0; --muted:#6b7280; --dim:rgba(255,255,255,.12);
  --accent:#6366f1; --accent2:#8b5cf6;
  --grad:linear-gradient(135deg,#6366f1 0%,#8b5cf6 100%);
  --glow:0 0 22px rgba(99,102,241,.28);
  --crit:#ef4444; --crit-bg:rgba(239,68,68,.13);
  --high:#f59e0b; --high-bg:rgba(245,158,11,.13);
  --med:#10b981;  --med-bg:rgba(16,185,129,.13);
  --low:#6b7280;  --low-bg:rgba(107,114,128,.13);
  --memoir:#6366f1; --memoir-bg:rgba(99,102,241,.13);
  --danger:#ef4444; --overlay:rgba(0,0,0,.78);
  --r:14px; --rs:8px; --blur:blur(18px);
  --shadow:0 6px 32px rgba(0,0,0,.45); --tr:.18s ease;
}
[data-theme="light"] {
  --bg:#f3f4ff; --surface:rgba(255,255,255,.82); --surface2:rgba(255,255,255,.96);
  --border:rgba(99,102,241,.14); --border-h:rgba(99,102,241,.5);
  --text:#1a1b2e; --muted:#6b7280; --dim:rgba(0,0,0,.08);
  --accent:#4f46e5; --accent2:#7c3aed;
  --grad:linear-gradient(135deg,#4f46e5 0%,#7c3aed 100%);
  --glow:0 0 22px rgba(79,70,229,.18);
  --crit:#dc2626; --crit-bg:rgba(220,38,38,.1);
  --high:#d97706; --high-bg:rgba(217,119,6,.1);
  --med:#059669;  --med-bg:rgba(5,150,105,.1);
  --low:#9ca3af;  --low-bg:rgba(156,163,175,.1);
  --memoir:#4f46e5; --memoir-bg:rgba(79,70,229,.1);
  --danger:#dc2626; --overlay:rgba(0,0,0,.42);
  --shadow:0 6px 32px rgba(0,0,0,.13);
}
/* ── RESET ──────────────────────────────────────────── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{height:100%}
body{background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden;
  font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;}
button{cursor:pointer;font-family:inherit;border:none;background:none;}
input,select{font-family:inherit;outline:none;}
/* ── BACKGROUND ART ─────────────────────────────────── */
.bg-art{position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden;}
.orb{position:absolute;border-radius:50%;filter:blur(90px);
  animation:float 14s ease-in-out infinite;}
.orb-1{width:640px;height:520px;opacity:.28;
  background:radial-gradient(circle,rgba(99,102,241,.7) 0%,transparent 70%);
  top:-18%;left:-8%;animation-delay:0s;}
.orb-2{width:500px;height:620px;opacity:.22;
  background:radial-gradient(circle,rgba(139,92,246,.65) 0%,transparent 70%);
  top:28%;right:-6%;animation-delay:-6s;}
.orb-3{width:420px;height:420px;opacity:.18;
  background:radial-gradient(circle,rgba(99,102,241,.5) 0%,transparent 70%);
  bottom:-12%;left:38%;animation-delay:-11s;}
[data-theme="light"] .orb{opacity:.12;}
@keyframes float{
  0%,100%{transform:translate(0,0) scale(1);}
  33%{transform:translate(22px,-28px) scale(1.04);}
  66%{transform:translate(-16px,18px) scale(.97);}
}
.bg-grid{position:fixed;inset:0;z-index:0;pointer-events:none;
  background-image:linear-gradient(rgba(255,255,255,.016) 1px,transparent 1px),
    linear-gradient(90deg,rgba(255,255,255,.016) 1px,transparent 1px);
  background-size:44px 44px;}
[data-theme="light"] .bg-grid{
  background-image:linear-gradient(rgba(99,102,241,.04) 1px,transparent 1px),
    linear-gradient(90deg,rgba(99,102,241,.04) 1px,transparent 1px);}
/* ── LAYOUT ─────────────────────────────────────────── */
#app{position:relative;z-index:1;display:flex;flex-direction:column;min-height:100vh;}
/* ── HEADER ─────────────────────────────────────────── */
header{position:sticky;top:0;z-index:50;height:62px;
  backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  background:rgba(8,11,18,.86);border-bottom:1px solid var(--border);
  padding:0 26px;display:flex;align-items:center;gap:12px;}
[data-theme="light"] header{background:rgba(243,244,255,.9);}
.logo{display:flex;align-items:center;gap:10px;text-decoration:none;}
.logo-icon{width:32px;height:32px;background:var(--grad);border-radius:9px;
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 0 18px rgba(99,102,241,.35);flex-shrink:0;}
.logo-icon svg{width:18px;height:18px;}
.logo-name{font-size:17px;font-weight:760;letter-spacing:-.03em;
  background:var(--grad);-webkit-background-clip:text;
  -webkit-text-fill-color:transparent;background-clip:text;}
.logo-ver{background:var(--surface2);border:1px solid var(--border);
  border-radius:999px;padding:2px 8px;font-size:11px;color:var(--muted);
  display:none;}
@media(min-width:520px){.logo-ver{display:block;}}
.hgap{flex:1;}
.status-pill{display:flex;align-items:center;gap:7px;background:var(--surface);
  border:1px solid var(--border);border-radius:999px;padding:5px 12px;
  font-size:12px;color:var(--muted);}
.sdot{width:7px;height:7px;border-radius:50%;background:var(--med);
  animation:pulse-dot 2.6s ease-in-out infinite;}
@keyframes pulse-dot{0%,100%{box-shadow:0 0 5px var(--med);}
  50%{box-shadow:0 0 13px var(--med),0 0 4px var(--med);}}
.hdr-sel{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--rs);padding:7px 10px;color:var(--text);font-size:13px;
  cursor:pointer;transition:border-color var(--tr);max-width:140px;}
.hdr-sel:hover,.hdr-sel:focus{border-color:var(--border-h);}
.hdr-btn{display:flex;align-items:center;gap:6px;background:var(--surface);
  border:1px solid var(--border);border-radius:var(--rs);padding:7px 12px;
  color:var(--text);font-size:13px;transition:all var(--tr);}
.hdr-btn svg{width:14px;height:14px;flex-shrink:0;}
.hdr-btn:hover{background:var(--surface2);border-color:var(--border-h);
  color:var(--accent);box-shadow:var(--glow);}
.hdr-btn.ico{padding:7px;}
/* ── MAIN ───────────────────────────────────────────── */
main{max-width:1060px;margin:0 auto;width:100%;padding:26px 22px 64px;}
/* ── STATS ──────────────────────────────────────────── */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));
  gap:12px;margin-bottom:22px;}
.stat-card{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:18px 18px 14px;
  backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  transition:all var(--tr);position:relative;overflow:hidden;}
.stat-card::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:var(--grad);opacity:0;transition:opacity var(--tr);}
.stat-card:hover{border-color:var(--border-h);transform:translateY(-2px);
  box-shadow:var(--shadow);}
.stat-card:hover::after{opacity:1;}
.si{width:34px;height:34px;border-radius:9px;background:var(--surface2);
  border:1px solid var(--border);display:flex;align-items:center;
  justify-content:center;margin-bottom:11px;color:var(--accent);}
.si svg{width:16px;height:16px;}
.sv{font-size:28px;font-weight:760;letter-spacing:-.045em;line-height:1;
  background:var(--grad);-webkit-background-clip:text;
  -webkit-text-fill-color:transparent;background-clip:text;}
.sl{font-size:11px;color:var(--muted);text-transform:uppercase;
  letter-spacing:.07em;margin-top:4px;}
/* ── SEARCH ─────────────────────────────────────────── */
.search-wrap{display:flex;gap:9px;align-items:stretch;
  margin-bottom:18px;flex-wrap:wrap;}
.search-field{flex:1;min-width:210px;position:relative;display:flex;}
.search-field svg{position:absolute;left:13px;top:50%;
  transform:translateY(-50%);color:var(--muted);width:15px;height:15px;}
.search-field input{width:100%;background:var(--surface);
  border:1px solid var(--border);border-radius:var(--rs);
  padding:10px 13px 10px 38px;color:var(--text);font-size:14px;
  transition:all var(--tr);}
.search-field input::placeholder{color:var(--muted);}
.search-field input:focus{border-color:var(--accent);background:var(--surface2);
  box-shadow:0 0 0 3px rgba(99,102,241,.16);}
.btn-p{display:inline-flex;align-items:center;gap:6px;
  background:var(--grad);border-radius:var(--rs);padding:10px 17px;
  color:#fff;font-size:14px;font-weight:620;white-space:nowrap;
  transition:all var(--tr);}
.btn-p:hover{filter:brightness(1.1);box-shadow:var(--glow);transform:translateY(-1px);}
.btn-p:active{transform:none;}
.btn-g{display:inline-flex;align-items:center;gap:6px;
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--rs);padding:10px 15px;color:var(--text);
  font-size:14px;white-space:nowrap;transition:all var(--tr);}
.btn-g:hover{background:var(--surface2);border-color:var(--border-h);}
.btn-d{display:inline-flex;align-items:center;gap:6px;
  background:var(--crit-bg);border:1px solid rgba(239,68,68,.28);
  border-radius:var(--rs);padding:9px 14px;color:var(--crit);
  font-size:13px;transition:all var(--tr);}
.btn-d:hover{background:rgba(239,68,68,.22);}
.btn-sm{padding:6px 11px;font-size:13px;}
/* ── SECTION ROW ────────────────────────────────────── */
.sec-row{display:flex;align-items:center;gap:9px;margin:2px 0 13px;}
.sec-title{font-size:11px;font-weight:720;text-transform:uppercase;
  letter-spacing:.08em;color:var(--muted);}
.sec-chip{background:var(--surface2);border:1px solid var(--border);
  border-radius:999px;padding:2px 9px;font-size:11px;color:var(--muted);}
/* ── MEMORY CARDS ───────────────────────────────────── */
.mem-list{display:flex;flex-direction:column;gap:8px;}
.mem{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:13px 15px;
  display:flex;gap:13px;align-items:flex-start;
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  transition:all var(--tr);position:relative;overflow:hidden;}
.mem::before{content:'';position:absolute;left:0;top:0;bottom:0;
  width:3px;border-radius:3px 0 0 3px;}
.mem.ic::before{background:var(--crit);}
.mem.ih::before{background:var(--high);}
.mem.im::before{background:var(--med);}
.mem.il::before{background:var(--low);}
.mem.io::before{background:var(--memoir);}
.mem:hover{border-color:var(--border-h);transform:translateX(3px);
  box-shadow:var(--shadow);}
.mem:hover .mdel{opacity:1;transform:scale(1);}
.badge{flex:none;font-size:10px;font-weight:720;padding:3px 9px;
  border-radius:999px;text-transform:uppercase;letter-spacing:.04em;
  margin-top:1px;white-space:nowrap;}
.b-critical{background:var(--crit-bg);color:var(--crit);
  box-shadow:0 0 8px rgba(239,68,68,.18);}
.b-high{background:var(--high-bg);color:var(--high);
  box-shadow:0 0 8px rgba(245,158,11,.14);}
.b-medium{background:var(--med-bg);color:var(--med);}
.b-low{background:var(--low-bg);color:var(--low);}
.b-memoir{background:var(--memoir-bg);color:var(--memoir);}
.mbody{flex:1;min-width:0;}
.mtopic{font-weight:650;font-size:14px;}
.msum{color:var(--muted);margin-top:3px;font-size:13.5px;
  line-height:1.5;word-break:break-word;}
.mmeta{color:var(--dim);font-size:11px;margin-top:6px;
  display:flex;gap:8px;flex-wrap:wrap;}
.mdel{opacity:0;transform:scale(.82);flex:none;
  width:30px;height:30px;display:flex;align-items:center;
  justify-content:center;border-radius:var(--rs);
  border:1px solid transparent;color:var(--danger);
  transition:all var(--tr);align-self:flex-start;background:transparent;}
.mdel:hover{background:var(--crit-bg);border-color:rgba(239,68,68,.3);}
.mdel svg{width:14px;height:14px;}
/* ── EMPTY / SKELETON ───────────────────────────────── */
.empty-state{text-align:center;padding:56px 20px;color:var(--muted);}
.empty-state svg{width:44px;height:44px;color:var(--dim);
  margin:0 auto 14px;display:block;}
.empty-state p{font-size:15px;margin-bottom:4px;}
.empty-state small{font-size:13px;color:var(--dim);}
.skel{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:16px 15px;margin-bottom:8px;}
.sl-line{height:11px;border-radius:4px;
  background:linear-gradient(90deg,var(--surface2) 25%,var(--surface) 50%,var(--surface2) 75%);
  background-size:400% 100%;animation:shimmer 1.7s infinite;}
.sl-line+.sl-line{margin-top:9px;}
@keyframes shimmer{0%{background-position:100% 0}100%{background-position:-100% 0}}
/* ── MODAL ──────────────────────────────────────────── */
.moverlay{position:fixed;inset:0;background:var(--overlay);display:none;
  align-items:center;justify-content:center;padding:20px;z-index:200;
  backdrop-filter:blur(5px);-webkit-backdrop-filter:blur(5px);}
.moverlay.open{display:flex;}
.mcard{background:var(--bg);border:1px solid var(--border);border-radius:18px;
  max-width:680px;width:100%;max-height:82vh;display:flex;flex-direction:column;
  box-shadow:0 28px 90px rgba(0,0,0,.65);animation:min .22s ease;overflow:hidden;}
@keyframes min{from{opacity:0;transform:scale(.94) translateY(10px);}
  to{opacity:1;transform:none;}}
.mhd{padding:18px 22px 15px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;}
.mhd h3{font-size:15px;font-weight:650;}
.mclose{width:28px;height:28px;display:flex;align-items:center;
  justify-content:center;background:var(--surface);border:1px solid var(--border);
  border-radius:7px;color:var(--muted);font-size:17px;transition:all var(--tr);}
.mclose:hover{color:var(--text);border-color:var(--border-h);}
.mbod{flex:1;overflow-y:auto;padding:18px 22px;}
.mft{padding:14px 22px;border-top:1px solid var(--border);
  display:flex;justify-content:flex-end;gap:9px;}
.clu-card{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);margin-bottom:9px;overflow:hidden;}
.clu-head{padding:11px 15px;display:flex;align-items:center;gap:9px;
  border-bottom:1px solid var(--border);background:var(--surface2);}
.clu-num{background:var(--grad);color:#fff;border-radius:999px;
  width:21px;height:21px;display:flex;align-items:center;justify-content:center;
  font-size:10px;font-weight:720;flex-shrink:0;}
.clu-ttl{font-weight:620;font-size:13px;flex:1;}
.clu-cnt{font-size:11px;color:var(--muted);background:var(--surface);
  border:1px solid var(--border);border-radius:999px;padding:1px 8px;}
.clu-item{padding:9px 15px;border-bottom:1px solid var(--border);font-size:12.5px;}
.clu-item:last-child{border-bottom:none;}
.clu-item strong{color:var(--text);}
.clu-item span{color:var(--muted);}
/* ── CONFIRM ────────────────────────────────────────── */
.cov{position:fixed;inset:0;z-index:300;background:var(--overlay);display:none;
  align-items:center;justify-content:center;padding:20px;
  backdrop-filter:blur(5px);-webkit-backdrop-filter:blur(5px);}
.cov.open{display:flex;}
.ccard{background:var(--bg);border:1px solid var(--border);border-radius:16px;
  padding:26px 26px 22px;max-width:360px;width:100%;
  box-shadow:0 28px 80px rgba(0,0,0,.55);animation:min .2s ease;}
.ccard h4{font-size:16px;margin-bottom:7px;}
.ccard p{font-size:13.5px;color:var(--muted);line-height:1.5;margin-bottom:18px;}
.cacts{display:flex;gap:9px;justify-content:flex-end;}
/* ── TOASTS ─────────────────────────────────────────── */
#tc{position:fixed;bottom:22px;right:22px;z-index:500;
  display:flex;flex-direction:column;gap:7px;pointer-events:none;}
.toast{background:var(--surface2);border:1px solid var(--border);border-radius:10px;
  padding:11px 15px;font-size:13.5px;
  backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);
  box-shadow:var(--shadow);pointer-events:all;
  display:flex;align-items:center;gap:9px;
  min-width:220px;max-width:360px;animation:tin .23s ease;}
@keyframes tin{from{opacity:0;transform:translateX(14px);}to{opacity:1;transform:none;}}
.toast.ok{border-color:rgba(16,185,129,.35);}
.toast.err{border-color:rgba(239,68,68,.35);}
.tico{width:15px;height:15px;flex-shrink:0;}
.toast.ok .tico{color:var(--med);}
.toast.err .tico{color:var(--crit);}
/* ── SCROLLBAR ──────────────────────────────────────── */
::-webkit-scrollbar{width:5px;height:5px;}
::-webkit-scrollbar-track{background:transparent;}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px;}
::-webkit-scrollbar-thumb:hover{background:var(--muted);}
</style>
</head>
<body>
<div class="bg-art" aria-hidden="true">
  <div class="orb orb-1"></div><div class="orb orb-2"></div><div class="orb orb-3"></div>
</div>
<div class="bg-grid" aria-hidden="true"></div>
<div id="app">

<!-- HEADER -->
<header>
  <div class="logo">
    <div class="logo-icon">
      <svg viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="3.5" cy="7" r="2.2" fill="white" opacity=".92"/>
        <circle cx="9" cy="3.5" r="2.2" fill="white" opacity=".92"/>
        <circle cx="14.5" cy="7" r="2.2" fill="white" opacity=".92"/>
        <circle cx="6" cy="13" r="2.2" fill="white" opacity=".92"/>
        <circle cx="12" cy="13" r="2.2" fill="white" opacity=".92"/>
        <line x1="3.5" y1="7" x2="9" y2="3.5" stroke="white" stroke-width="1.1" opacity=".45"/>
        <line x1="9" y1="3.5" x2="14.5" y2="7" stroke="white" stroke-width="1.1" opacity=".45"/>
        <line x1="3.5" y1="7" x2="6" y2="13" stroke="white" stroke-width="1.1" opacity=".45"/>
        <line x1="14.5" y1="7" x2="12" y2="13" stroke="white" stroke-width="1.1" opacity=".45"/>
        <line x1="6" y1="13" x2="12" y2="13" stroke="white" stroke-width="1.1" opacity=".45"/>
        <line x1="9" y1="3.5" x2="6" y2="13" stroke="white" stroke-width="1.1" opacity=".25"/>
        <line x1="9" y1="3.5" x2="12" y2="13" stroke="white" stroke-width="1.1" opacity=".25"/>
        <line x1="3.5" y1="7" x2="14.5" y2="7" stroke="white" stroke-width="1.1" opacity=".25"/>
      </svg>
    </div>
    <span class="logo-name">noshy</span>
    <span class="logo-ver">v0.2.0</span>
  </div>
  <div class="hgap"></div>
  <div class="status-pill"><span class="sdot"></span><span>Live</span></div>
  <select class="hdr-sel" id="projectFilter" aria-label="Filter by project">
    <option value="">All projects</option>
  </select>
  <button class="hdr-btn" id="clusterBtn">
    <svg viewBox="0 0 15 15" fill="none" stroke="currentColor" stroke-width="1.5">
      <circle cx="4" cy="4" r="2"/><circle cx="11" cy="4" r="2"/>
      <circle cx="4" cy="11" r="2"/><circle cx="11" cy="11" r="2"/>
      <path d="M6 4h3M7.5 6v3M4 6v2M11 6v2M6 11h3"/>
    </svg>
    Clusters
  </button>
  <button class="hdr-btn ico" id="themeBtn" aria-label="Toggle theme">
    <svg id="themeIco" viewBox="0 0 15 15" fill="none" stroke="currentColor" stroke-width="1.5">
      <circle cx="7.5" cy="7.5" r="3"/>
      <path d="M7.5 1v1.5M7.5 12.5V14M1 7.5h1.5M12.5 7.5H14M3.2 3.2l1 1M10.8 10.8l1 1M3.2 11.8l1-1M10.8 4.2l1-1"/>
    </svg>
  </button>
</header>

<!-- MAIN -->
<main>
  <!-- Stats -->
  <div class="stats-grid">
    <div class="stat-card">
      <div class="si"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M8 2C5.2 2 3 4.2 3 7c0 1.9 1 3.6 2.6 4.5V13h4.8v-1.5C12 10.6 13 8.9 13 7c0-2.8-2.2-5-5-5z"/>
        <path d="M5.5 13h5"/></svg></div>
      <div class="sv" id="s-mem">—</div><div class="sl">Memories</div>
    </div>
    <div class="stat-card">
      <div class="si"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
        <rect x="2" y="2" width="12" height="12" rx="1.5"/>
        <path d="M5 6h6M5 9h4"/></svg></div>
      <div class="sv" id="s-moir">—</div><div class="sl">Memoirs</div>
    </div>
    <div class="stat-card">
      <div class="si"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
        <circle cx="8" cy="8" r="5.5"/><circle cx="8" cy="8" r="1.8"/>
        <path d="M8 2.5v1.2M8 12.3v1.2M2.5 8h1.2M12.3 8h1.2"/></svg></div>
      <div class="sv" id="s-con">—</div><div class="sl">Concepts</div>
    </div>
    <div class="stat-card">
      <div class="si"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
        <circle cx="3.5" cy="8" r="2"/><circle cx="12.5" cy="8" r="2"/>
        <circle cx="8" cy="3.5" r="2"/><circle cx="8" cy="12.5" r="2"/>
        <path d="M5.4 8h5.2M8 5.4v5.2"/></svg></div>
      <div class="sv" id="s-edg">—</div><div class="sl">Edges</div>
    </div>
    <div class="stat-card">
      <div class="si"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M2 12 L5 7 L8 9.5 L11 5 L14 8"/>
        <path d="M2 14h12"/></svg></div>
      <div class="sv" id="s-wt">—</div><div class="sl">Avg Weight</div>
    </div>
  </div>

  <!-- Search -->
  <div class="search-wrap">
    <div class="search-field">
      <svg viewBox="0 0 15 15" fill="none" stroke="currentColor" stroke-width="1.6">
        <circle cx="6.5" cy="6.5" r="4.5"/><path d="M10 10l3 3"/></svg>
      <input id="q" type="text" placeholder="Search memories &amp; memoirs…"
        autocomplete="off" spellcheck="false">
    </div>
    <button class="btn-p" id="searchBtn" onclick="search()">
      <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.6" style="width:13px;height:13px">
        <circle cx="6" cy="6" r="4"/><path d="M9 9l3 3"/></svg>
      Search
    </button>
    <button class="btn-g" onclick="clearSearch()">Clear</button>
  </div>

  <!-- Section header -->
  <div class="sec-row">
    <span class="sec-title" id="listTitle">Recent memories</span>
    <span class="sec-chip" id="secChip" style="display:none"></span>
  </div>

  <!-- Memory list -->
  <div id="list" class="mem-list"></div>
</main>
</div>

<!-- CLUSTERS MODAL -->
<div class="moverlay" id="clusterModal"
  onclick="if(event.target.id==='clusterModal')closeClusters()">
  <div class="mcard">
    <div class="mhd">
      <h3>Near-Duplicate Clusters</h3>
      <button class="mclose" onclick="closeClusters()" aria-label="Close">&#x2715;</button>
    </div>
    <div class="mbod" id="clusterBody">
      <div class="empty-state"><p>Scanning…</p></div>
    </div>
    <div class="mft">
      <button class="btn-g btn-sm" onclick="closeClusters()">Close</button>
      <button class="btn-p btn-sm" id="consolidateBtn" onclick="runConsolidate()"
        style="display:none">Consolidate all</button>
    </div>
  </div>
</div>

<!-- CONFIRM DIALOG -->
<div class="cov" id="confirmOv">
  <div class="ccard">
    <h4 id="cfTitle">Are you sure?</h4>
    <p id="cfMsg"></p>
    <div class="cacts">
      <button class="btn-g btn-sm" id="cfNo">Cancel</button>
      <button class="btn-d btn-sm" id="cfYes">Delete</button>
    </div>
  </div>
</div>

<!-- TOASTS -->
<div id="tc"></div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>(s==null?'':String(s)).replace(/[&<>"']/g,c=>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

/* ── theme ─────────────────────────────────────────── */
const sunSVG=`<svg viewBox="0 0 15 15" fill="none" stroke="currentColor" stroke-width="1.5">
  <circle cx="7.5" cy="7.5" r="3"/>
  <path d="M7.5 1v1.5M7.5 12.5V14M1 7.5h1.5M12.5 7.5H14M3.2 3.2l1 1M10.8 10.8l1 1M3.2 11.8l1-1M10.8 4.2l1-1"/>
</svg>`;
const moonSVG=`<svg viewBox="0 0 15 15" fill="none" stroke="currentColor" stroke-width="1.5">
  <path d="M11.5 10A6 6 0 015 3.5a6 6 0 100 9 6 6 0 006.5-2.5z"/>
</svg>`;
function applyTheme(t){
  document.documentElement.setAttribute('data-theme',t);
  $('themeBtn').innerHTML=t==='dark'?sunSVG:moonSVG;
  try{localStorage.setItem('noshy.theme',t);}catch(_){}
}
(function(){
  let t;try{t=localStorage.getItem('noshy.theme');}catch(_){}
  if(!t)t=window.matchMedia('(prefers-color-scheme:light)').matches?'light':'dark';
  applyTheme(t);
})();
$('themeBtn').addEventListener('click',()=>{
  applyTheme(document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark');
});

/* ── toasts ─────────────────────────────────────────── */
function toast(msg,type='ok',ms=3200){
  const iko=type==='ok'
    ?`<svg class="tico" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2 7l3.5 3.5L12 3.5"/></svg>`
    :`<svg class="tico" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 3l8 8M11 3l-8 8"/></svg>`;
  const el=document.createElement('div');
  el.className=`toast ${type}`;
  el.innerHTML=iko+`<span>${esc(msg)}</span>`;
  $('tc').prepend(el);
  setTimeout(()=>{el.style.cssText='opacity:0;transform:translateX(12px);transition:all .28s ease;';
    setTimeout(()=>el.remove(),290);},ms);
}

/* ── confirm ─────────────────────────────────────────── */
function confirm2(title,msg){
  return new Promise(res=>{
    $('cfTitle').textContent=title;$('cfMsg').textContent=msg;
    $('confirmOv').classList.add('open');
    const no=$('cfNo'),yes=$('cfYes');
    function done(v){
      $('confirmOv').classList.remove('open');
      no.removeEventListener('click',onNo);yes.removeEventListener('click',onYes);
      res(v);
    }
    const onNo=()=>done(false),onYes=()=>done(true);
    no.addEventListener('click',onNo);yes.addEventListener('click',onYes);
  });
}

/* ── stats ─────────────────────────────────────────── */
function animN(el,to){
  const from=parseInt(el.textContent)||0,diff=to-from;
  if(!diff){el.textContent=to;return;}
  const dur=380,t0=performance.now();
  const tick=now=>{
    const p=Math.min(1,(now-t0)/dur);
    el.textContent=Math.round(from+diff*(p<.5?2*p*p:-1+(4-2*p)*p));
    if(p<1)requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}
async function loadStats(){
  try{
    const s=await(await fetch('/stats')).json();
    animN($('s-mem'),s.memory_count||0);animN($('s-moir'),s.memoir_count||0);
    animN($('s-con'),s.concept_count||0);animN($('s-edg'),s.edge_count||0);
    $('s-wt').textContent=(s.avg_weight||0).toFixed(2);
  }catch(e){console.error('stats',e);}
}

/* ── projects ─────────────────────────────────────────── */
async function loadProjects(){
  try{
    const r=await(await fetch('/projects')).json();
    const sel=$('projectFilter'),cur=sel.value;
    sel.innerHTML='<option value="">All projects</option>'+
      (r.projects||[]).map(p=>
        `<option value="${esc(p.project)}">${esc(p.project)} (${p.memory_count})</option>`).join('');
    if(cur)sel.value=cur;
  }catch(_){}
}

/* ── render ─────────────────────────────────────────── */
function impCls(imp){
  const i=(imp||'medium').toLowerCase();
  return {critical:'ic',high:'ih',medium:'im',low:'il',memoir:'io'}[i]||'im';
}
function render(items){
  $('secChip').style.display=items.length?'':'none';
  if(items.length)$('secChip').textContent=items.length+(items.length===1?' result':' results');
  if(!items.length){
    $('list').innerHTML=`<div class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <circle cx="11" cy="11" r="7"/><path d="M16.5 16.5l4 4"/>
        <path d="M8 11h6M11 8v6"/>
      </svg>
      <p>No memories found</p>
      <small>Try a different query or store some memories first.</small>
    </div>`;
    return;
  }
  $('list').innerHTML=items.map(m=>{
    const imp=(m._kind==='memoir'||m.importance==='memoir')?'memoir':(m.importance||'medium').toLowerCase();
    const when=(m.created_at||'').slice(0,10);
    const proj=m.project&&m.project!=='default'?m.project:'';
    const w=m.weight!=null?Number(m.weight).toFixed(2):'';
    const topic=m.topic||m.title||'(untitled)';
    const summary=m.summary||m.content||'';
    const meta=[];
    if(when)meta.push(`<svg viewBox="0 0 11 11" fill="none" stroke="currentColor" stroke-width="1.4" style="width:9px;height:9px"><rect x="1" y="1.5" width="9" height="8.5" rx="1.2"/><path d="M1 4.5h9M3.5.5v2M7.5.5v2"/></svg> ${esc(when)}`);
    if(proj)meta.push(`<svg viewBox="0 0 11 11" fill="none" stroke="currentColor" stroke-width="1.4" style="width:9px;height:9px"><path d="M1.5 7.5V4L5.5 2 9.5 4v3.5L5.5 9z"/></svg> ${esc(proj)}`);
    if(w)meta.push(`w ${w}`);
    if(m.access_count)meta.push(`${m.access_count}×`);
    const del=m.id
      ?`<button class="mdel" data-id="${esc(m.id)}" data-topic="${esc(topic)}" aria-label="Delete">
          <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M2 3.5h10M5 3.5V2.5h4v1M5.5 6v4.5M8.5 6v4.5M3.5 3.5l.5 8h6l.5-8"/>
          </svg></button>`:'';
    return `<div class="mem ${impCls(imp)}">
      <span class="badge b-${imp}">${imp}</span>
      <div class="mbody">
        <div class="mtopic">${esc(topic)}</div>
        <div class="msum">${esc(summary)}</div>
        ${meta.length?`<div class="mmeta">${meta.map(x=>`<span>${x}</span>`).join('')}</div>`:''}
      </div>${del}</div>`;
  }).join('');
  $('list').querySelectorAll('.mdel').forEach(b=>b.addEventListener('click',onDelete));
}

function showSkel(){
  $('list').innerHTML=[1,2,3,4].map(()=>`
    <div class="skel">
      <div class="sl-line" style="width:55%;height:13px"></div>
      <div class="sl-line" style="width:88%;margin-top:10px"></div>
      <div class="sl-line" style="width:36%;margin-top:8px;height:9px;opacity:.5"></div>
    </div>`).join('');
  $('secChip').style.display='none';
}

/* ── delete ─────────────────────────────────────────── */
async function onDelete(e){
  const btn=e.currentTarget;
  const id=btn.dataset.id,topic=btn.dataset.topic||'this memory';
  const ok=await confirm2(`Delete memory?`,`"${topic}" will be permanently removed.`);
  if(!ok)return;
  try{
    const r=await fetch('/memories/'+encodeURIComponent(id),{method:'DELETE'});
    if(!r.ok){toast('Delete failed ('+r.status+')','err');return;}
    const row=btn.closest('.mem');
    row.style.cssText='opacity:0;transform:translateX(-10px);transition:all .2s ease;';
    setTimeout(()=>{row.remove();loadStats();},200);
    toast('Memory deleted');
  }catch(err){toast('Delete failed: '+err,'err');}
}

/* ── search / load ─────────────────────────────────── */
function qp(extra){
  const proj=$('projectFilter').value;
  const u=new URLSearchParams(extra||{});
  if(proj)u.set('project',proj);u.set('limit','50');return u.toString();
}
async function loadRecent(){
  $('listTitle').textContent='Recent memories'+($('projectFilter').value?' · '+$('projectFilter').value:'');
  showSkel();
  try{const r=await(await fetch('/memories?'+qp())).json();render(r.memories||[]);}
  catch(_){$('list').innerHTML='<div class="empty-state"><p>Failed to load</p></div>';}
}
async function search(){
  const q=$('q').value.trim();
  if(!q){loadRecent();return;}
  $('listTitle').textContent='Results for "'+q+'"'+($('projectFilter').value?' in '+$('projectFilter').value:'');
  showSkel();
  try{const r=await(await fetch('/memories?'+qp({q}))).json();render(r.memories||[]);}
  catch(_){$('list').innerHTML='<div class="empty-state"><p>Search failed</p></div>';}
}
function clearSearch(){$('q').value='';$('projectFilter').value='';loadRecent();}

/* ── clusters ─────────────────────────────────────── */
async function openClusters(){
  $('clusterModal').classList.add('open');
  $('clusterBody').innerHTML='<div class="empty-state"><p>Scanning for near-duplicates…</p></div>';
  $('consolidateBtn').style.display='none';
  try{
    const proj=$('projectFilter').value;
    const r=await(await fetch('/clusters?threshold=0.85'+(proj?'&project='+encodeURIComponent(proj):'')+'')).json();
    const clusters=r.clusters||[];
    if(!clusters.length){
      $('clusterBody').innerHTML=`<div class="empty-state">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M5 13l4 4L19 7"/></svg>
        <p>No near-duplicates found</p><small>Your memory store is clean.</small></div>`;
      return;
    }
    $('consolidateBtn').style.display='';
    $('clusterBody').innerHTML=clusters.map((c,i)=>`
      <div class="clu-card">
        <div class="clu-head">
          <span class="clu-num">${i+1}</span>
          <span class="clu-ttl">Cluster ${i+1}</span>
          <span class="clu-cnt">${c.length} memories</span>
        </div>
        ${c.slice(0,5).map(m=>`
          <div class="clu-item">
            <strong>${esc(m.topic||'')}</strong>
            <span>: ${esc((m.summary||'').slice(0,160))}</span>
          </div>`).join('')}
        ${c.length>5?`<div class="clu-item" style="font-style:italic;color:var(--muted)">&#8230;and ${c.length-5} more</div>`:''}
      </div>`).join('');
  }catch(e){$('clusterBody').innerHTML='<div class="empty-state"><p>Failed to load clusters</p></div>';}
}
function closeClusters(){$('clusterModal').classList.remove('open');}
async function runConsolidate(){
  const ok=await confirm2('Consolidate all clusters?',
    'Each cluster of near-duplicates will be merged into one. Duplicates will be permanently deleted.');
  if(!ok)return;
  const btn=$('consolidateBtn');btn.disabled=true;btn.textContent='Consolidating…';
  try{
    const r=await(await fetch('/tools/call',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:'noshy_consolidate_clusters',
        arguments:{threshold:0.85,project:$('projectFilter').value||undefined}})
    })).json();
    toast((r.content&&r.content[0]&&r.content[0].text)||'Done.');
    closeClusters();loadStats();loadProjects();loadRecent();
  }catch(e){toast('Consolidate failed: '+e,'err');}
  finally{btn.disabled=false;btn.textContent='Consolidate all';}
}

/* ── keyboard ─────────────────────────────────────── */
$('q').addEventListener('keydown',e=>{
  if(e.key==='Enter')search();
  if(e.key==='Escape')clearSearch();
});
$('projectFilter').addEventListener('change',()=>$('q').value?search():loadRecent());
$('clusterBtn').addEventListener('click',openClusters);

/* ── boot ─────────────────────────────────────────── */
loadStats();loadProjects();loadRecent();
setInterval(loadStats,15000);setInterval(loadProjects,30000);
</script>
</body>
</html>"""


# ──────────── HTTP API mode ────────────

def run_http(host: str = "127.0.0.1", port: int = 8720, db_path: str = None):
    """Run Noshy as an HTTP API server with graceful shutdown."""
    global store
    import hmac, signal
    embedder = auto_embedder()
    store = NoshyStore(db_path=db_path, embedder=embedder)

    auth_token = os.environ.get("NOSHY_HTTP_TOKEN", "")
    if auth_token:
        log.info("HTTP auth enabled (Bearer token required)")
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
                    self._send_html(200, DASHBOARD_HTML)
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
    parser = argparse.ArgumentParser(description="Noshy — MCP-native memory for AI agents")
    parser.add_argument("--db", help="Database path", default=None)
    sub = parser.add_subparsers(dest="command")

    # stdio mode (MCP)
    sub.add_parser("mcp", help="Run as MCP stdio server")

    # HTTP mode
    http_p = sub.add_parser("http", help="Run as HTTP API server")
    http_p.add_argument("--host", default="127.0.0.1")
    http_p.add_argument("--port", type=int, default=8720)

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

    _add_json(sub.add_parser("purge", help="Delete expired memories now"))
    _add_json(sub.add_parser("sweep", help="Run the full maintenance sweep (purge + decay + consolidate)"))

    # "serve" is a friendly alias for "http"
    serve_p = sub.add_parser("serve", help="Alias for `http` — start the HTTP server + dashboard")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8720)

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
    store = NoshyStore(db_path=db, embedder=auto_embedder())

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
    elif args.command == "purge":
        n = store.purge_expired()
        _out([f"Purged {n} expired memories"], {"purged": n})
    elif args.command == "sweep":
        from hooks import daily_sweep
        # daily_sweep instantiates its own store, but we already opened the DB;
        # it'll honor NOSHY_DB if set, so just call it.
        result = daily_sweep()
        _out([f"Sweep: purged={result['purged']}, "
              f"consolidated={result['consolidated']}, "
              f"clusters={result.get('clusters', 0)}",
              f"Store: {result['stats']['memory_count']} memories, "
              f"avg weight {(result['stats']['avg_weight'] or 0):.2f}"],
             result)


if __name__ == "__main__":
    main()
