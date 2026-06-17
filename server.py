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
        "serverInfo": {"name": "noshy", "version": "0.1.0"},
    }


def handle_tools_list(params: Dict = None) -> Dict:
    return {"tools": MCP_TOOLS}


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

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Noshy — Memory Dashboard</title>
<style>
  :root {
    --bg:#0e1117; --panel:#161b22; --border:#272e3a; --text:#e6edf3;
    --muted:#8b949e; --accent:#5b8def; --crit:#f85149; --high:#d29922;
    --med:#3fb950; --low:#6e7681;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:20px 28px; border-bottom:1px solid var(--border);
    display:flex; align-items:center; gap:14px; }
  header h1 { margin:0; font-size:20px; font-weight:650; }
  header .dot { width:10px; height:10px; border-radius:50%; background:var(--med); }
  .wrap { max-width:1100px; margin:0 auto; padding:24px 28px; }
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
    gap:14px; margin-bottom:24px; }
  .stat { background:var(--panel); border:1px solid var(--border); border-radius:10px;
    padding:16px 18px; }
  .stat .n { font-size:26px; font-weight:700; }
  .stat .l { color:var(--muted); font-size:12px; text-transform:uppercase;
    letter-spacing:.04em; margin-top:4px; }
  .searchbar { display:flex; gap:10px; margin-bottom:18px; }
  .searchbar input { flex:1; background:var(--panel); border:1px solid var(--border);
    border-radius:8px; padding:10px 14px; color:var(--text); font-size:14px; }
  .searchbar button { background:var(--accent); border:0; border-radius:8px;
    color:#fff; padding:0 18px; font-weight:600; cursor:pointer; }
  .searchbar button:hover { filter:brightness(1.08); }
  .mem { background:var(--panel); border:1px solid var(--border); border-radius:10px;
    padding:14px 16px; margin-bottom:10px; display:flex; gap:14px; align-items:flex-start; }
  .badge { flex:none; font-size:10px; font-weight:700; padding:3px 8px; border-radius:5px;
    text-transform:uppercase; letter-spacing:.03em; margin-top:2px; }
  .b-critical{background:rgba(248,81,73,.16);color:var(--crit);}
  .b-high{background:rgba(210,153,34,.16);color:var(--high);}
  .b-medium{background:rgba(63,185,80,.16);color:var(--med);}
  .b-low{background:rgba(110,118,129,.16);color:var(--low);}
  .b-memoir{background:rgba(91,141,239,.16);color:var(--accent);}
  .mem .body { flex:1; min-width:0; }
  .mem .topic { font-weight:600; }
  .mem .summary { color:var(--muted); margin-top:2px; word-wrap:break-word; }
  .mem .meta { color:var(--low); font-size:11px; margin-top:6px; }
  .empty { color:var(--muted); text-align:center; padding:40px; }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.05em;
    color:var(--muted); margin:24px 0 12px; }
</style>
</head>
<body>
<header><span class="dot"></span><h1>Noshy</h1>
  <span style="color:var(--muted)">persistent memory dashboard</span></header>
<div class="wrap">
  <div class="stats" id="stats"></div>
  <div class="searchbar">
    <input id="q" placeholder="Search memories &amp; memoirs…" autofocus>
    <button onclick="search()">Search</button>
  </div>
  <h2 id="listTitle">Recent memories</h2>
  <div id="list"><div class="empty">Loading…</div></div>
</div>
<script>
const esc = s => (s||"").replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function loadStats(){
  try {
    const s = await (await fetch('/stats')).json();
    const cards = [
      ['memory_count','Memories'],['memoir_count','Memoirs'],
      ['concept_count','Concepts'],['edge_count','Edges']
    ];
    document.getElementById('stats').innerHTML = cards.map(([k,l]) =>
      `<div class="stat"><div class="n">${s[k]??0}</div><div class="l">${l}</div></div>`
    ).join('') +
      `<div class="stat"><div class="n">${(s.avg_weight??0).toFixed(2)}</div>`+
      `<div class="l">Avg weight</div></div>`;
  } catch(e){ console.error(e); }
}
function render(items, kindFromBadge){
  const list = document.getElementById('list');
  if(!items.length){ list.innerHTML='<div class="empty">Nothing here yet.</div>'; return; }
  list.innerHTML = items.map(m => {
    const imp = (m._kind==='memoir') ? 'memoir'
      : (m.importance||'medium').toLowerCase();
    const when = (m.created_at||'').slice(0,10);
    const proj = (m.project && m.project!=='default') ? ' · '+esc(m.project) : '';
    const w = (m.weight!=null) ? ' · w'+Number(m.weight).toFixed(2) : '';
    return `<div class="mem"><span class="badge b-${imp}">${imp}</span>`+
      `<div class="body"><div class="topic">${esc(m.topic||m.title||'')}</div>`+
      `<div class="summary">${esc(m.summary||m.content||'')}</div>`+
      `<div class="meta">${when}${proj}${w}</div></div></div>`;
  }).join('');
}
async function loadRecent(){
  document.getElementById('listTitle').textContent='Recent memories';
  try {
    const r = await (await fetch('/memories?limit=25')).json();
    render(r.memories||[]);
  } catch(e){ document.getElementById('list').innerHTML=
    '<div class="empty">Failed to load.</div>'; }
}
async function search(){
  const q = document.getElementById('q').value.trim();
  if(!q){ loadRecent(); return; }
  document.getElementById('listTitle').textContent='Results for "'+q+'"';
  try {
    const res = await (await fetch('/tools/call',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:'noshy_recall',
        arguments:{query:q, mode:'hybrid', limit:25}})})).json();
    // recall returns formatted text; re-query /memories filtered client-side is
    // not ideal, so parse the text blocks back into cards.
    const text = (res.content && res.content[0] && res.content[0].text) || '';
    if(text==='No memories found.'){ render([]); return; }
    const blocks = text.split('\\n\\n').map(b=>{
      const lines=b.split('\\n');
      const m=lines[0].match(/^\\[(\\w+)\\]\\s+(.*)$/);
      return { importance:(m?m[1]:'medium').toLowerCase(),
        _kind:(m&&m[1].toLowerCase()==='memoir')?'memoir':null,
        topic:m?m[2]:lines[0], summary:lines.slice(1).join(' ') };
    });
    render(blocks);
  } catch(e){ document.getElementById('list').innerHTML=
    '<div class="empty">Search failed.</div>'; }
}
document.getElementById('q').addEventListener('keydown',e=>{ if(e.key==='Enter') search(); });
loadStats(); loadRecent();
setInterval(loadStats, 15000);
</script>
</body>
</html>"""


# ──────────── HTTP API mode ────────────

def run_http(host: str = "127.0.0.1", port: int = 8720, db_path: str = None):
    """Run Noshy as an HTTP API server."""
    global store
    import hmac
    embedder = auto_embedder()
    store = NoshyStore(db_path=db_path, embedder=embedder)

    auth_token = os.environ.get("NOSHY_HTTP_TOKEN", "")
    if auth_token:
        log.info("HTTP auth enabled (Bearer token required)")
    public_paths = {"/health"}

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

                # Public paths bypass auth; everything else requires it when configured
                if path not in ("/", "/dashboard", "/health") and not self._require_auth():
                    return

                if path in ("/", "/dashboard"):
                    self._send_html(200, DASHBOARD_HTML)
                elif path == "/stats":
                    self._send_json(200, store.get_stats())
                elif path == "/memories":
                    limit = int(qs.get("limit", ["25"])[0])
                    limit = max(1, min(limit, 200))
                    project = qs.get("project", [None])[0]
                    sql = ("SELECT id, created_at, topic, summary, importance, weight, "
                           "project, access_count FROM memories "
                           "WHERE (expires_at IS NULL OR expires_at > ?)")
                    params = [_now_iso()]
                    if project:
                        sql += " AND project = ?"
                        params.append(project)
                    sql += " ORDER BY created_at DESC LIMIT ?"
                    params.append(limit)
                    rows = [dict(r) for r in store.conn.execute(sql, params).fetchall()]
                    self._send_json(200, {"memories": rows})
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down Noshy HTTP API")
        server.shutdown()


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
