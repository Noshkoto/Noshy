"""
Noshy context — session-aware memory for Hermes Agent.

Features for Hermes users:
- Session-start context injection ("previously on...")
- Decision timeline tracking
- Pattern detection across sessions
- Project-aware recall
- Preference extraction
"""
import os
import sys
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from store import NoshyStore
from embed import auto_embedder

log = logging.getLogger("aion.context")

_shared_store: Optional[NoshyStore] = None


def _get_store() -> NoshyStore:
    global _shared_store
    if _shared_store is None:
        _shared_store = NoshyStore(embedder=auto_embedder())
    return _shared_store

# ──────────── Session Start Context ────────────

def session_context(*, project: str = None, max_memories: int = 10,
                    last_session: str = None, user_name: str = None) -> str:
    """
    Generate context for a new Hermes session.
    Injects critical/high memories, recent decisions, and user preferences.

    Args:
        project: Filter to specific project (None = all)
        max_memories: Max memories to include
        last_session: ISO timestamp of last session end
        user_name: User's name for personalization

    Returns:
        Formatted context string ready to inject into Hermes
    """
    store = _get_store()

    sections = []

    # 1. Critical memories (never miss these)
    critical = _fetch_important(store, "critical", limit=3, project=project)
    if critical:
        sections.append("CRITICAL CONTEXT — Do NOT forget:")
        for m in critical:
            sections.append(f"  - {m['summary']}")

    # 2. Recent decisions (what we resolved)
    decisions = _fetch_decisions(store, limit=5, since=last_session, project=project)
    if decisions:
        sections.append("\nRECENT DECISIONS:")
        for m in decisions:
            when = m['created_at'][:10] if m.get('created_at') else ''
            sections.append(f"  [{when}] {m['topic']}: {m['summary']}")

    # 3. Active work (high importance, recent)
    active = _fetch_important(store, "high", limit=max_memories, project=project,
                               since=last_session)
    if active:
        sections.append("\nACTIVE CONTEXT:")
        for m in active:
            sections.append(f"  - {m['summary']}")

    # 4. Project overview
    if project:
        project_memories = _fetch_project_overview(store, project)
        if project_memories:
            sections.append(f"\nPROJECT '{project}' OVERVIEW:")
            sections.append(f"  {project_memories['memory_count']} memories stored")
            sections.append(f"  Recent activity: {project_memories.get('last_activity', 'unknown')}")
            if project_memories.get('top_topics'):
                sections.append(f"  Top topics: {', '.join(project_memories['top_topics'][:5])}")

    # 5. User preferences (persistent)
    prefs = _fetch_preferences(store, user_name)
    if prefs:
        sections.append("\nYOUR PREFERENCES:")
        for p in prefs[:5]:
            sections.append(f"  - {p['summary']}")

    # 6. What's new since last session
    if last_session:
        new_count = _count_since(store, last_session, project)
        if new_count > 0:
            sections.insert(0, f"Since your last session: {new_count} new memories recorded.\n")

    if not sections:
        return "No prior context found. Noshy memory is ready."

    return "\n".join(sections)


# ──────────── Decision Timeline ────────────

def decision_timeline(*, project: str = None, days: int = 30, limit: int = 20) -> str:
    """
    Generate a decision audit trail — every choice, fix, and resolution.

    Args:
        project: Filter to specific project
        days: Look back this many days
        limit: Max decisions to show

    Returns:
        Chronological decision timeline
    """
    store = _get_store()

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else None

    query = """
    SELECT topic, summary, importance, source, created_at, project
    FROM memories
    WHERE (importance IN ('critical', 'high')
           OR topic LIKE '%decision%'
           OR topic LIKE '%decide%'
           OR topic LIKE '%fix-%'
           OR topic LIKE '%resolved%')
    """
    params = []

    if since:
        query += " AND created_at >= ?"
        params.append(since)
    if project:
        query += " AND project = ?"
        params.append(project)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = store.conn.execute(query, params).fetchall()

    if not rows:
        return "No decisions recorded."

    lines = [f"DECISION TIMELINE ({'project: ' + project if project else 'all projects'}):"]
    current_day = None

    for row in rows:
        day = row["created_at"][:10] if row["created_at"] else "unknown"
        if day != current_day:
            current_day = day
            lines.append(f"\n-- {day} --")
        imp = row["importance"].upper() if row["importance"] else "MEDIUM"
        proj = f"[{row['project']}]" if row["project"] and row["project"] != "default" else ""
        lines.append(f"  [{imp}] {row['topic']} {proj}: {row['summary']}")

    return "\n".join(lines)


# ──────────── Pattern Detection ────────────

def detect_patterns(*, project: str = None, min_occurrences: int = 3) -> List[Dict]:
    """
    Find repeated solutions across sessions — candidates for skill generation.

    Args:
        project: Filter to specific project
        min_occurrences: Min times a pattern must appear

    Returns:
        List of detected patterns with topic, frequency, and suggested action
    """
    store = _get_store()

    query = """
    SELECT topic, COUNT(*) as occurrences, GROUP_CONCAT(summary, ' | ') as summaries,
           MAX(created_at) as last_seen, MIN(created_at) as first_seen
    FROM memories
    WHERE topic NOT LIKE '%test%'
    """
    params = []

    if project:
        query += " AND project = ?"
        params.append(project)

    query += """
    GROUP BY topic
    HAVING occurrences >= ?
    ORDER BY occurrences DESC
    """
    params.append(min_occurrences)

    rows = store.conn.execute(query, params).fetchall()

    patterns = []
    for row in rows:
        summaries = row["summaries"].split(" | ") if row["summaries"] else []
        # Check if summaries are genuinely similar (Jaccard)
        if len(summaries) >= 2:
            words_a = set(summaries[0].lower().split())
            words_b = set(summaries[-1].lower().split())
            overlap = len(words_a & words_b)
            union = len(words_a | words_b)
            if union > 0 and overlap / union < 0.3:
                continue  # False grouping — topic same but different content

        patterns.append({
            "topic": row["topic"],
            "occurrences": row["occurrences"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "suggested_action": "Create a skill for this pattern" if row["occurrences"] >= 5 else "Consider documenting",
        })

    return patterns


# ──────────── Preference Extraction ────────────

def extract_preferences(transcript: str) -> List[Dict]:
    """
    Extract user preferences from a transcript.
    Detects: code style, naming conventions, tool choices, communication preferences.

    Args:
        transcript: Conversation text

    Returns:
        List of extracted preferences ready to store
    """
    # Lightweight detection — no LLM needed for common patterns
    prefs = []

    # Code style preferences
    style_patterns = [
        (r"(?:i prefer|use|let'?s use|stick to|go with)\s+([\w\s]+?)(?:[.;,]|$)", "style"),
        (r"(?:always|never|don'?t)\s+(use|call|name|write)\s+([\w\s]+?)(?:[.;,]|$)", "convention"),
        (r"(?:my preference is|i like to|i'd rather)\s+([\w\s]+?)(?:[.;,]|$)", "preference"),
        (r"instead of\s+([\w\s]+?)(?:[.;,]|$)\s*(?:use|try|go with|switch to)\s+([\w\s]+?)(?:[.;,]|$)", "tradeoff"),
    ]

    import re
    for pattern, category in style_patterns:
        for match in re.finditer(pattern, transcript, re.IGNORECASE):
            text = match.group(0).strip()
            if len(text) > 10:
                prefs.append({
                    "topic": f"pref-{category}",
                    "summary": text,
                    "importance": "high",
                    "keywords": [category, "preference"],
                    "source": "pref-detect",
                })

    # Tool preferences
    tool_patterns = [
        (r"(?:i use|using|switched to|moved to)\s+(claude|codex|copilot|hermes|vscode|neovim|zed|windsurf)", "tool-choice"),
        (r"(?:prefer|favor)\s+(python|rust|go|typescript|javascript)(?:\s|$)", "language-pref"),
    ]

    for pattern, category in tool_patterns:
        for match in re.finditer(pattern, transcript, re.IGNORECASE):
            text = match.group(0).strip()
            prefs.append({
                "topic": f"pref-{category}",
                "summary": text,
                "importance": "medium",
                "keywords": [category, "tooling"],
                "source": "pref-detect",
            })

    return prefs[:5]


# ──────────── Helpers ────────────

def _fetch_important(store: NoshyStore, importance: str, limit: int = 10,
                     project: str = None, since: str = None) -> List[Dict]:
    query = "SELECT * FROM memories WHERE importance = ?"
    params = [importance]

    if project:
        query += " AND project = ?"
        params.append(project)
    if since:
        query += " AND created_at >= ?"
        params.append(since)

    query += " ORDER BY weight DESC, created_at DESC LIMIT ?"
    params.append(limit)

    return [dict(r) for r in store.conn.execute(query, params).fetchall()]


def _fetch_decisions(store: NoshyStore, limit: int = 10, since: str = None,
                     project: str = None) -> List[Dict]:
    query = """
    SELECT * FROM memories
    WHERE (topic LIKE '%decision%' OR topic LIKE '%decide%' OR topic LIKE '%choose%'
           OR topic LIKE '%fix-%' OR topic LIKE '%resolved%' OR importance IN ('critical', 'high'))
    """
    params = []

    if since:
        query += " AND created_at >= ?"
        params.append(since)
    if project:
        query += " AND project = ?"
        params.append(project)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    return [dict(r) for r in store.conn.execute(query, params).fetchall()]


def _fetch_preferences(store: NoshyStore, user_name: str = None) -> List[Dict]:
    query = """
    SELECT * FROM memories
    WHERE topic LIKE 'pref-%'
    ORDER BY weight DESC, created_at DESC LIMIT 10
    """
    return [dict(r) for r in store.conn.execute(query).fetchall()]


def _fetch_project_overview(store: NoshyStore, project: str) -> Dict:
    count = store.conn.execute(
        "SELECT COUNT(*) FROM memories WHERE project = ?", [project]
    ).fetchone()[0]
    last = store.conn.execute(
        "SELECT MAX(created_at) FROM memories WHERE project = ?", [project]
    ).fetchone()[0]
    topics = store.conn.execute(
        "SELECT topic, COUNT(*) as c FROM memories WHERE project = ? GROUP BY topic ORDER BY c DESC LIMIT 5",
        [project]
    ).fetchall()

    return {
        "memory_count": count,
        "last_activity": last,
        "top_topics": [t["topic"] for t in topics],
    }


def _count_since(store: NoshyStore, since: str, project: str = None) -> int:
    query = "SELECT COUNT(*) FROM memories WHERE created_at >= ?"
    params = [since]
    if project:
        query += " AND project = ?"
        params.append(project)
    return store.conn.execute(query, params).fetchone()[0]


# ──────────── CLI ────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Noshy Context for Hermes")
    sub = parser.add_subparsers(dest="cmd")

    ctx = sub.add_parser("session-start", help="Generate session-start context")
    ctx.add_argument("--project")
    ctx.add_argument("--last-session")
    ctx.add_argument("--user")

    dec = sub.add_parser("timeline", help="Decision timeline")
    dec.add_argument("--project")
    dec.add_argument("--days", type=int, default=30)

    pat = sub.add_parser("patterns", help="Detect skill-worthy patterns")
    pat.add_argument("--project")
    pat.add_argument("--min", type=int, default=3)

    args = parser.parse_args()

    if args.cmd == "session-start":
        print(session_context(project=args.project, last_session=args.last_session, user_name=args.user))
    elif args.cmd == "timeline":
        print(decision_timeline(project=args.project, days=args.days))
    elif args.cmd == "patterns":
        patterns = detect_patterns(project=args.project, min_occurrences=args.min)
        for p in patterns:
            print(f"\n{p['topic']} ({p['occurrences']}x)")
            print(f"  {p['first_seen'][:10]} → {p['last_seen'][:10]}")
            print(f"  → {p['suggested_action']}")
