#!/usr/bin/env python3
"""
Noshy Hermes Skill — memory operations as native Hermes Agent tools.
Drop this into your Hermes workflow for persistent cross-session memory.
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from store import NoshyStore
from embed import auto_embedder

# Singleton store
_store: NoshyStore = None

def get_store():
    global _store
    if _store is None:
        db_path = os.environ.get("NOSHY_DB", os.path.expanduser("~/.noshy/memories.db"))
        _store = NoshyStore(db_path=db_path, embedder=auto_embedder())
    return _store


# ──────────── Tool Functions (called by Hermes) ────────────

def noshy_remember(topic: str, summary: str, *, keywords: list = None, importance: str = "medium", project: str = "default") -> str:
    """Store a memory. Use this to remember ANYTHING that matters — decisions, facts, bugs, fixes, user preferences, project state.
    
    Args:
        topic: Short topic slug in kebab-case (e.g., 'auth-fix', 'deploy-config')
        summary: One sentence factual summary of what to remember
        keywords: List of search keywords for recall
        importance: critical, high, medium, or low
        project: Project name to filter by
    """
    store = get_store()
    mid = store.store_memory(topic=topic, summary=summary, keywords=keywords, importance=importance, project=project)
    return f"Memory stored: {mid}"

def noshy_recall(query: str, *, project: str = None, limit: int = 15) -> str:
    """Search and recall memories. Use this before starting any task or when you need context.
    
    Args:
        query: What to search for — topic, keyword, or natural language
        project: Optional project filter
        limit: Max results (default 15)
    """
    store = get_store()
    results = store.recall_hybrid(query, limit=limit, project=project)
    if not results:
        return "No memories found."
    
    lines = []
    for i, r in enumerate(results, 1):
        imp = r.get('importance', 'medium').upper()
        topic = r.get('topic', 'unknown')
        summary = r.get('summary', '')
        project = r.get('project', '')
        lines.append(f"{i}. [{imp}] {topic}" + (f" [{project}]" if project else "") + f"\n   {summary}")
    return "\n\n".join(lines)

def noshy_learn(title: str, content: str, *, project: str = "default") -> str:
    """Store permanent knowledge — documentation, reference material, facts that don't expire.
    
    Args:
        title: Title of the knowledge entry
        content: Full content of what to learn
        project: Project name
    """
    store = get_store()
    mid = store.store_memoir(title=title, content=content, project=project)
    return f"Memoir stored: {mid}"

def noshy_summary(project: str = None) -> str:
    """Get a summary of what's in memory. Call this at the start of a session to know what happened before.
    
    Args:
        project: Optional project filter
    """
    store = get_store()
    stats = store.get_stats()
    
    # Get recent critical/high memories
    import sqlite3
    query = """
    SELECT topic, summary, importance, project FROM memories
    WHERE weight > 0.5
    """
    params = []
    if project:
        query += " AND project = ?"
        params.append(project)
    query += " ORDER BY created_at DESC LIMIT 20"
    
    rows = store.conn.execute(query, params).fetchall()
    
    lines = [f"Noshy memory — {stats['memory_count']} memories, {stats['memoir_count']} memoirs, {stats['concept_count']} concepts"]
    lines.append("--- Recent memories ---")
    for r in rows:
        imp = (r['importance'] or 'medium').upper()
        lines.append(f"[{imp}] {r['topic']}: {r['summary']}")

    return "\n".join(lines)

def noshy_link(source_query: str, target_query: str, relation: str = "related") -> str:
    """Link two memories together by searching for them first.
    
    Args:
        source_query: Search query for the source memory
        target_query: Search query for the target memory
        relation: Relationship type (related, extends, depends_on, contradicts, caused_by)
    """
    store = get_store()
    sources = store.recall_hybrid(source_query, limit=1)
    targets = store.recall_hybrid(target_query, limit=1)
    
    if not sources or not targets:
        return "Could not find one or both memories."
    
    store.link_memories(sources[0]["id"], targets[0]["id"], relation=relation)
    return f"Linked '{sources[0]['topic']}' --{relation}--> '{targets[0]['topic']}'"


# ──────────── If run directly ────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Noshy Hermes Skill CLI")
    sub = parser.add_subparsers(dest="cmd")
    
    p = sub.add_parser("recall")
    p.add_argument("query")
    p.add_argument("--project")
    print(noshy_recall(**{k:v for k,v in vars(parser.parse_args()).items() if k in ['query','project']}))
