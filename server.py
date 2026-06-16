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

from store import NoshyStore
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
                "importance": {"type": "string", "enum": ["critical", "high", "medium", "low"], "default": "medium"},
                "project": {"type": "string", "default": "default"},
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
            )
            return {"content": [{"type": "text", "text": f"Memory stored: {mid}"}]}

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

            out = "\n\n".join(
                f"[{r.get('importance', 'medium').upper()}] {r['topic']}\n{r['summary']}"
                for r in results
            )
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


# ──────────── HTTP API mode ────────────

def run_http(host: str = "127.0.0.1", port: int = 8720, db_path: str = None):
    """Run Noshy as an HTTP API server."""
    global store
    embedder = auto_embedder()
    store = NoshyStore(db_path=db_path, embedder=embedder)

    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, payload: Dict):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
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

        def do_GET(self):
            try:
                if self.path == "/stats":
                    self._send_json(200, store.get_stats())
                elif self.path == "/tools/list":
                    self._send_json(200, {"tools": MCP_TOOLS})
                elif self.path == "/health":
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

    server = HTTPServer((host, port), Handler)
    log.info(f"Noshy HTTP API running on http://{host}:{port}")
    server.serve_forever()


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

    # CLI commands
    sub.add_parser("stats", help="Show memory stats")
    recall_p = sub.add_parser("recall", help="Recall memories")
    recall_p.add_argument("query")
    recall_p.add_argument("--project", default=None)
    recall_p.add_argument("--limit", type=int, default=15)

    store_p = sub.add_parser("store", help="Store a memory")
    store_p.add_argument("topic")
    store_p.add_argument("summary")
    store_p.add_argument("--importance", default="medium")
    store_p.add_argument("--project", default="default")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    db = getattr(args, 'db', None)

    if args.command == "mcp":
        run_stdio(db_path=db)
        return
    if args.command == "http":
        run_http(args.host, args.port, db_path=db)
        return

    global store
    store = NoshyStore(db_path=db, embedder=auto_embedder())

    if args.command == "import":
        count = store.import_icm(args.icm_path)
        print(f"Imported {count} memories from {args.icm_path}")
    elif args.command == "stats":
        stats = store.get_stats()
        for k, v in stats.items():
            print(f"{k}: {v}")
    elif args.command == "recall":
        results = store.recall_hybrid(args.query, limit=args.limit, project=args.project)
        for i, r in enumerate(results, 1):
            print(f"{i}. [{(r.get('importance') or 'm').upper()}] {r['topic']}")
            print(f"   {r['summary']}\n")
    elif args.command == "store":
        mid = store.store_memory(
            topic=args.topic, summary=args.summary,
            importance=args.importance, project=args.project,
        )
        print(f"Stored: {mid}")


if __name__ == "__main__":
    main()
