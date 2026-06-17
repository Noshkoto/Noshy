"""
Noshy session hooks — automatic memory extraction.
Drop this into Hermes as a hook plugin to auto-extract
memories at session end without manual calls.
"""
import os
import sys
import json
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import NoshyStore
from extractor import extract_facts
from embed import auto_embedder

log = logging.getLogger("aion.hook")

# ──────────── Session-End Hook ────────────

def on_session_end(transcript: str, *, project: str = "default", max_memories: int = 8) -> dict:
    """
    Auto-extract memories when a session ends.
    Call this from a Hermes session-end hook or any MCP client.

    Args:
        transcript: Full conversation transcript
        project: Project name for filtering
        max_memories: Max memories to extract per session

    Returns:
        dict with extracted count, memory IDs, and concepts found
    """
    if len(transcript.strip()) < 100:
        return {"extracted": 0, "reason": "transcript too short"}

    log.info(f"Extracting from {len(transcript)} chars...")

    embedder = auto_embedder()
    store = NoshyStore(embedder=embedder)

    facts = extract_facts(transcript)

    stored = []
    linked = []
    concepts = []

    for fact in facts:
        if fact.get("_type") == "relationship":
            store.link_memories(
                fact["source_id"],
                fact["target_id"],
                fact.get("relation", "related"),
            )
            linked.append(f"{fact['source_id'][:8]}->{fact['target_id'][:8]}")

        elif fact.get("_type") == "concept":
            concepts.append(fact["name"])

        else:
            # Check for existing memories first
            sid = store.store_memory(
                topic=fact["topic"],
                summary=fact["summary"],
                keywords=fact.get("keywords"),
                importance=fact.get("importance", "medium"),
                source="auto-hook",
                project=project,
                raw_excerpt=fact.get("raw_excerpt"),
            )
            stored.append(sid)

    log.info(f"Session end: {len(stored)} memories, {len(linked)} links, {len(concepts)} concepts")

    return {
        "extracted": len(stored),
        "memory_ids": stored,
        "relationships": len(linked),
        "concepts": concepts,
    }


# ──────────── Hermes Hook Integration ────────────

# Hermes hooks are Python scripts that get called with stdin JSON.
# Register this file as a hook in Hermes config:
#
# hooks:
#   session_end:
#     command: "python3"
#     args: ["/home/openclaw/noshy/hooks.py", "session-end"]

def _hermes_hook_handler():
    """Handle Hermes hook invocation via stdin JSON."""
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        log.error("Failed to parse hook input")
        sys.exit(1)

    hook_type = data.get("hook", "")
    session = data.get("session", {})
    messages = data.get("messages", [])

    # Build transcript from messages
    transcript_parts = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                item.get("text", "") for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        if content and isinstance(content, str):
            transcript_parts.append(f"[{role}]: {content}")

    transcript = "\n\n".join(transcript_parts)

    if hook_type == "session_end" and transcript:
        project = data.get("project", session.get("project", "default"))
        result = on_session_end(transcript, project=project)
        print(json.dumps(result))
    else:
        print(json.dumps({"status": "skipped", "reason": f"no handler for hook={hook_type}"}))

    sys.exit(0)


# ──────────── Manual Daily Sweep ────────────

def daily_sweep(project: str = None):
    """
    Run a daily maintenance sweep:
    1. Purge expired memories
    2. Decay old memory weights
    3. Consolidate related memories
    4. Report stats

    Call this from a cron job or scheduled task.
    """
    store = NoshyStore()

    # Purge anything past its TTL
    purged = store.purge_expired()

    # Decay
    store.decay_weights(decay_rate=0.95)

    # Consolidate topics with 3+ memories
    # Find topics with multiple memories
    rows = store.conn.execute("""
    SELECT topic, COUNT(*) as cnt, AVG(weight) as avg_w
    FROM memories
    WHERE merged_from IS NULL
    GROUP BY topic HAVING cnt >= 3
    ORDER BY cnt DESC LIMIT 10
    """).fetchall()

    consolidated = 0
    for row in rows:
        try:
            n = store.consolidate(row["topic"], min_weight=0.3)
            consolidated += n
        except Exception as e:
            log.warning(f"Consolidation failed for {row['topic']}: {e}")

    # Cross-topic similarity consolidation (best-effort; needs embeddings)
    cluster_stats = {"clusters": 0, "merged": 0}
    try:
        cluster_stats = store.consolidate_clusters(threshold=0.88)
        consolidated += cluster_stats["merged"]
    except Exception as e:
        log.debug(f"Cluster consolidation skipped: {e}")

    stats = store.get_stats()

    avg_w = stats['avg_weight'] or 0.0
    log.info(f"Daily sweep: {purged} expired purged, decay applied, {consolidated} memories consolidated")
    log.info(f"Store: {stats['memory_count']} memories, avg weight {avg_w:.2f}")

    return {
        "purged": purged,
        "consolidated": consolidated,
        "clusters": cluster_stats["clusters"],
        "stats": stats,
    }


# ──────────── CLI Entrypoint ────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Noshy hooks")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("session-end", help="Run session-end extraction (reads stdin JSON)")
    sub.add_parser("daily-sweep", help="Run daily maintenance sweep")

    # Direct extraction from file
    extract_p = sub.add_parser("extract-file", help="Extract from a transcript file")
    extract_p.add_argument("file", help="Path to transcript file")
    extract_p.add_argument("--project", default="default")

    args = parser.parse_args()

    if args.cmd == "session-end":
        _hermes_hook_handler()
    elif args.cmd == "daily-sweep":
        result = daily_sweep()
        print(json.dumps(result, indent=2))
    elif args.cmd == "extract-file":
        with open(args.file) as f:
            transcript = f.read()
        result = on_session_end(transcript, project=args.project)
        print(json.dumps(result, indent=2))
