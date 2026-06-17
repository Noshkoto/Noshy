"""
Noshy — persistent memory for AI agents.
ICM-compatible schema with improvements:
- LLM-powered fact extraction (vs rule-based)
- Graph relationship tracking
- Smarter decay and consolidation
- MCP-native, Hermes-native
"""
import os
import re
import json
import time
import sqlite3
import hashlib
import logging
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

log = logging.getLogger("aion.store")


def _utcnow_iso() -> str:
    """UTC timestamp in ISO-8601 (timezone-aware, no microseconds)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

# ────────────────────────────────────────────────────────────────
# Constants (ICM-compatible)
# ────────────────────────────────────────────────────────────────
DEFAULT_EMBEDDING_DIMS = 768  # multilingual-e5-base

SELECT_MEMORY_COLS = """
    id, created_at, updated_at, last_accessed, access_count,
    weight, topic, summary, raw_excerpt, keywords, embedding,
    importance, source, project, expires_at
"""

SELECT_MEMOIR_COLS = """
    id, created_at, updated_at, last_accessed, access_count,
    title, content, content_hash, embedding, source, project
"""

SELECT_CONCEPT_COLS = """
    id, name, category, description, embedding, created_at, updated_at
"""


class NoshyStore:
    """Core memory store. Drop-in compatible with ICM's SQLite schema."""

    def __init__(self, db_path: str = None, embedding_dims: int = DEFAULT_EMBEDDING_DIMS, embedder=None):
        if db_path is None:
            db_path = os.environ.get("NOSHY_DB") or os.path.expanduser("~/.noshy/memories.db")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedding_dims = embedding_dims
        self.embedder = embedder
        # Each thread gets its own SQLite connection. WAL mode (set per
        # connection) lets concurrent readers coexist with a single writer,
        # which is what makes the threaded HTTP server safe.
        self._local = threading.local()
        self._vec_supported = False
        self._connect()  # opens the connection and sets self._vec_supported
        self.vec_enabled = self._vec_supported
        self._init_schema()
        log.info(f"Noshy store ready: {self.db_path}")

    @property
    def conn(self) -> sqlite3.Connection:
        """Thread-local SQLite connection (created on first use per thread)."""
        return self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        self._vec_supported = self._try_load_sqlite_vec(conn)
        self._local.conn = conn
        return conn

    def _try_load_sqlite_vec(self, conn: sqlite3.Connection) -> bool:
        """Best-effort load of the sqlite-vec extension. Returns True on success."""
        try:
            import sqlite_vec  # type: ignore
        except ImportError:
            return False
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            return True
        except Exception as e:
            log.debug(f"sqlite-vec load failed: {e}")
            return False

    def _init_schema(self):
        conn = self.conn
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '',
            last_accessed TEXT NOT NULL,
            access_count INTEGER DEFAULT 0,
            weight REAL DEFAULT 1.0,
            topic TEXT NOT NULL,
            summary TEXT NOT NULL,
            raw_excerpt TEXT,
            keywords TEXT,
            embedding BLOB,
            importance TEXT DEFAULT 'medium',
            source TEXT DEFAULT 'aion',
            project TEXT DEFAULT 'default',
            expires_at TEXT,
            -- Graph fields (Noshy extension)
            parent_id TEXT,
            merged_from TEXT,
            consolidation_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS memoirs (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '',
            last_accessed TEXT NOT NULL,
            access_count INTEGER DEFAULT 0,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT,
            embedding BLOB,
            source TEXT DEFAULT 'aion',
            project TEXT DEFAULT 'default'
        );

        CREATE TABLE IF NOT EXISTS concepts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            category TEXT,
            description TEXT,
            embedding BLOB,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS concept_links (
            memory_id TEXT NOT NULL,
            concept_id TEXT NOT NULL,
            strength REAL DEFAULT 1.0,
            PRIMARY KEY (memory_id, concept_id),
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
            FOREIGN KEY (concept_id) REFERENCES concepts(id) ON DELETE CASCADE
        );

        -- Graph edges between memories (Noshy extension)
        CREATE TABLE IF NOT EXISTS memory_edges (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            relation TEXT DEFAULT 'related',
            strength REAL DEFAULT 0.5,
            created_at TEXT NOT NULL,
            PRIMARY KEY (source_id, target_id, relation),
            FOREIGN KEY (source_id) REFERENCES memories(id) ON DELETE CASCADE,
            FOREIGN KEY (target_id) REFERENCES memories(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            score INTEGER NOT NULL CHECK(score IN (-1, 1)),
            reason TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            project TEXT DEFAULT 'default',
            source TEXT DEFAULT 'aion',
            metadata TEXT
        );

        CREATE TABLE IF NOT EXISTS icm_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_extractions (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            raw_text TEXT NOT NULL,
            source TEXT DEFAULT 'unknown',
            created_at TEXT NOT NULL,
            extracted_at TEXT,
            status TEXT DEFAULT 'pending'
        );

        CREATE INDEX IF NOT EXISTS idx_memories_topic ON memories(topic);
        CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project);
        CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance);
        CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
        CREATE INDEX IF NOT EXISTS idx_memories_weight ON memories(weight);
        CREATE INDEX IF NOT EXISTS idx_concepts_name ON concepts(name);
        CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
        CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_extractions(status);
        """)

        # Create vec0 virtual table if the extension was loaded
        if self.vec_enabled:
            try:
                conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0(
                    memory_id TEXT PRIMARY KEY,
                    embedding float[{self.embedding_dims}] distance_metric=cosine
                )
                """)
                log.info("sqlite-vec enabled for vector search")
            except Exception as e:
                log.warning(f"sqlite-vec table create failed: {e}")
                self.vec_enabled = False
        else:
            log.info("sqlite-vec not available — using blob-based cosine similarity fallback")

    # ──────────── CRUD Operations ────────────

    def store_memory(
        self,
        topic: str,
        summary: str,
        *,
        raw_excerpt: str = None,
        keywords: List[str] = None,
        embedding: bytes = None,
        importance: str = "medium",
        source: str = "noshy",
        project: str = "default",
        parent_id: str = None,
        auto_embed: bool = True,
        ttl_seconds: int = None,
        expires_at: str = None,
    ) -> str:
        """Store a new episodic memory. Returns the memory ID.

        Pass ttl_seconds for a relative expiry, or expires_at for an explicit
        ISO-8601 timestamp. Expired memories are filtered from recall and
        removed by purge_expired().

        Pass importance="auto" to have the LLM score it (falls back to "medium"
        if no LLM endpoint is configured).
        """
        # Auto-classify importance via LLM if requested
        if importance == "auto":
            try:
                from extractor import predict_importance
                importance = predict_importance(topic, summary)
            except Exception as e:
                log.debug(f"predict_importance failed: {e}")
                importance = "medium"

        now = _utcnow_iso()
        memory_id = _ulid()
        kw_str = ",".join(keywords) if keywords else None

        if expires_at is None and ttl_seconds is not None:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
            ).replace(microsecond=0).isoformat()

        # Auto-embed if no embedding provided
        if auto_embed and embedding is None and self.embedder is not None:
            try:
                embeddings = self.embedder.embed([summary])
                if embeddings:
                    embedding = embeddings[0]
            except Exception as e:
                log.debug(f"Auto-embed failed: {e}")

        # Deduplication: check for similar existing memory
        dedup_id = self._find_duplicate(topic, summary, project)
        if dedup_id:
            self.conn.execute("""
            UPDATE memories SET weight = MIN(weight + 0.3, 3.0),
                updated_at = ?, last_accessed = ?, access_count = access_count + 1,
                summary = CASE WHEN length(?) > length(summary) THEN ? ELSE summary END
            WHERE id = ?
            """, (now, now, summary, summary, dedup_id))
            self.conn.commit()
            log.debug(f"Dedup: bumped weight on {dedup_id[:12]}")
            return dedup_id

        self.conn.execute("""
        INSERT INTO memories (id, created_at, updated_at, last_accessed, weight,
            topic, summary, raw_excerpt, keywords, embedding, importance, source, project, parent_id, expires_at)
        VALUES (?, ?, ?, ?, 1.0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (memory_id, now, now, now, topic, summary, raw_excerpt,
              kw_str, embedding, importance, source, project, parent_id, expires_at))

        # Store vector if available
        if embedding:
            self._store_vector(memory_id, embedding)

        self.conn.commit()
        return memory_id

    def store_memoir(
        self,
        title: str,
        content: str,
        *,
        embedding: bytes = None,
        source: str = "noshy",
        project: str = "default",
        auto_embed: bool = True,
    ) -> str:
        """Store permanent knowledge (memoir)."""
        now = _utcnow_iso()
        memoir_id = _ulid()
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        # Check for duplicate
        existing = self.conn.execute(
            "SELECT id FROM memoirs WHERE content_hash = ?",
            (content_hash,)
        ).fetchone()
        if existing:
            return existing["id"]

        # Auto-embed title+content so memoirs are semantically searchable
        if auto_embed and embedding is None and self.embedder is not None:
            try:
                vecs = self.embedder.embed([f"{title}\n{content}"])
                if vecs:
                    embedding = vecs[0]
            except Exception as e:
                log.debug(f"Memoir auto-embed failed: {e}")

        self.conn.execute("""
        INSERT INTO memoirs (id, created_at, updated_at, last_accessed,
            title, content, content_hash, embedding, source, project)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (memoir_id, now, now, now, title, content, content_hash, embedding, source, project))
        self.conn.commit()
        return memoir_id

    # ──────────── Search / Recall ────────────

    def recall_by_topic(self, topic: str, limit: int = 15, project: str = None) -> List[Dict]:
        """Keyword search on topic + summary."""
        query = """
        SELECT {} FROM memories WHERE (topic LIKE ? OR summary LIKE ? OR keywords LIKE ?)
        AND (expires_at IS NULL OR expires_at > ?)
        """.format(SELECT_MEMORY_COLS)
        params = [f"%{topic}%", f"%{topic}%", f"%{topic}%", _utcnow_iso()]

        if project:
            query += " AND project = ?"
            params.append(project)

        query += " ORDER BY weight * access_count DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        self._touch([r["id"] for r in rows])
        return [dict(r) for r in rows]

    def recall_semantic(self, embedding: bytes, limit: int = 15, project: str = None) -> List[Dict]:
        """Vector similarity search. Falls back to keyword if no vec index."""
        if not embedding:
            return []

        # Try vec0 first
        if self.vec_enabled:
            try:
                k = max(limit * 4, limit)
                project_filter = ""
                params: List[Any] = [embedding, k, _utcnow_iso()]
                if project:
                    project_filter = " AND m.project = ?"
                    params.append(project)
                rows = self.conn.execute(f"""
                SELECT m.*
                FROM vec_memories v
                JOIN memories m ON v.memory_id = m.id
                WHERE v.embedding MATCH ? AND k = ?
                AND (m.expires_at IS NULL OR m.expires_at > ?){project_filter}
                ORDER BY v.distance
                LIMIT ?
                """, params + [limit]).fetchall()
                if rows:
                    self._touch([r["id"] for r in rows])
                    return [dict(r) for r in rows]
            except Exception as e:
                log.debug(f"vec0 search failed, falling back: {e}")

        # Fallback: cosine similarity on blob
        query = ("SELECT {} FROM memories WHERE embedding IS NOT NULL "
                 "AND (expires_at IS NULL OR expires_at > ?)").format(SELECT_MEMORY_COLS)
        params = [_utcnow_iso()]
        if project:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY weight DESC"
        all_rows = self.conn.execute(query, params).fetchall()
        scored = []
        for row in all_rows:
            if row["embedding"]:
                score = _cosine_similarity(embedding, row["embedding"])
                scored.append((score, dict(row)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:limit]]

    def recall_memoirs(self, query: str, limit: int = 10, project: str = None,
                       embedding: bytes = None) -> List[Dict]:
        """Recall permanent knowledge (memoirs) by keyword, and by semantic
        similarity when an embedding (or an embedder + query) is available."""
        # Keyword layer
        sql = """
        SELECT id, created_at, title, content, source, project
        FROM memoirs WHERE (title LIKE ? OR content LIKE ?)
        """
        params = [f"%{query}%", f"%{query}%"]
        if project:
            sql += " AND project = ?"
            params.append(project)
        sql += " ORDER BY access_count DESC, created_at DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        found = {r["id"]: r for r in rows}

        # Semantic layer — auto-embed the query if we can
        if embedding is None and self.embedder is not None and query:
            try:
                vecs = self.embedder.embed([query])
                if vecs:
                    embedding = vecs[0]
            except Exception as e:
                log.debug(f"Memoir query embed failed: {e}")

        if embedding:
            esql = "SELECT id, created_at, title, content, source, project, embedding FROM memoirs WHERE embedding IS NOT NULL"
            eparams: List[Any] = []
            if project:
                esql += " AND project = ?"
                eparams.append(project)
            scored = []
            for row in self.conn.execute(esql, eparams).fetchall():
                if row["embedding"]:
                    score = _cosine_similarity(embedding, row["embedding"])
                    scored.append((score, row))
            scored.sort(key=lambda x: x[0], reverse=True)
            for _, row in scored[:limit]:
                if row["id"] not in found:
                    d = dict(row)
                    d.pop("embedding", None)
                    found[row["id"]] = d

        results = list(found.values())[:limit]

        ids = [r["id"] for r in results]
        if ids:
            now = _utcnow_iso()
            placeholders = ",".join("?" * len(ids))
            self.conn.execute(
                f"UPDATE memoirs SET last_accessed = ?, access_count = access_count + 1 "
                f"WHERE id IN ({placeholders})",
                [now, *ids],
            )
            self.conn.commit()
        return results

    def recall_hybrid(self, query: str, embedding: bytes = None, limit: int = 15,
                      project: str = None, include_memoirs: bool = True) -> List[Dict]:
        """Combine keyword + semantic + graph recall for best results.

        When include_memoirs is set, matching permanent-knowledge entries are
        appended (tagged with _kind='memoir') so stored docs are recallable.
        """
        results = {}

        # Age weights automatically (runs at most once per interval)
        try:
            self.maybe_decay()
        except Exception as e:
            log.debug(f"maybe_decay skipped: {e}")

        # Keyword layer
        for r in self.recall_by_topic(query, limit=limit, project=project):
            results[r["id"]] = r

        # Semantic layer — auto-embed the query if we have an embedder
        if embedding is None and self.embedder is not None and query:
            try:
                vecs = self.embedder.embed([query])
                if vecs:
                    embedding = vecs[0]
            except Exception as e:
                log.debug(f"Hybrid auto-embed failed: {e}")

        if embedding:
            for r in self.recall_semantic(embedding, limit=limit, project=project):
                if r["id"] not in results:
                    results[r["id"]] = r

        # Graph layer: pull in connected memories
        if results:
            id_list = list(results.keys())[:30]
            placeholders = ",".join("?" * len(id_list))
            try:
                graph_rows = self.conn.execute(f"""
                SELECT m.*, e.relation, e.strength as edge_strength
                FROM memory_edges e
                JOIN memories m ON (e.target_id = m.id OR e.source_id = m.id)
                WHERE (e.source_id IN ({placeholders}) OR e.target_id IN ({placeholders}))
                AND m.id NOT IN ({placeholders})
                ORDER BY e.strength DESC LIMIT ?
                """, id_list + id_list + id_list + [limit]).fetchall()
                for row in graph_rows:
                    rid = row["id"]
                    if rid not in results:
                        results[rid] = dict(row)
            except Exception as e:
                log.debug(f"Graph recall failed: {e}")

        ranked = sorted(results.values(), key=lambda r: r.get("weight") or 0, reverse=True)[:limit]

        # Memoir layer: append matching permanent knowledge (reuse query embedding)
        if include_memoirs:
            for m in self.recall_memoirs(query, limit=max(3, limit // 3),
                                         project=project, embedding=embedding):
                m["_kind"] = "memoir"
                m["topic"] = m.get("title", "memoir")
                m["summary"] = (m.get("content") or "")[:280]
                ranked.append(m)

        return ranked

    # ──────────── Graph Operations ────────────

    def link_memories(self, source_id: str, target_id: str, relation: str = "related", strength: float = 0.5):
        """Create a graph edge between two memories."""
        self.conn.execute("""
        INSERT OR REPLACE INTO memory_edges (source_id, target_id, relation, strength, created_at)
        VALUES (?, ?, ?, ?, ?)
        """, (source_id, target_id, relation, strength, _utcnow_iso()))
        self.conn.commit()

    def link_concept(self, memory_id: str, concept_name: str, category: str = None) -> str:
        """Link a memory to a concept, creating the concept if needed."""
        concept_id = _hash_id(concept_name.lower())
        now = _utcnow_iso()

        self.conn.execute("""
        INSERT OR IGNORE INTO concepts (id, name, category, description, created_at, updated_at)
        VALUES (?, ?, ?, '', ?, ?)
        """, (concept_id, concept_name.lower(), category, now, now))

        self.conn.execute("""
        INSERT OR REPLACE INTO concept_links (memory_id, concept_id, strength) VALUES (?, ?, 1.0)
        """, (memory_id, concept_id))
        self.conn.commit()
        return concept_id

    # ──────────── Maintenance ────────────

    def consolidate(self, topic: str, min_weight: float = 0.3) -> int:
        """Merge related memories on the same topic into a single survivor and
        delete the now-redundant originals (their edges/vectors go with them).

        Returns the number of memories removed.
        """
        rows = self.conn.execute("""
        SELECT * FROM memories WHERE topic = ? AND weight >= ? ORDER BY created_at
        """, (topic, min_weight)).fetchall()
        if len(rows) < 2:
            return 0

        # De-duplicate summaries while preserving order
        seen = set()
        parts = []
        for r in rows:
            s = (r["summary"] or "").strip()
            if s and s not in seen:
                seen.add(s)
                parts.append(s)
        merged = "; ".join(parts)

        survivor = rows[-1]
        merged_id = survivor["id"]
        gone = [r["id"] for r in rows[:-1]]
        # Survivor inherits the strongest weight in the group (capped)
        max_weight = min(max(r["weight"] for r in rows) + 0.2, 3.0)
        now = _utcnow_iso()

        # Re-point any graph edges from the doomed rows onto the survivor
        ph = ",".join("?" * len(gone))
        self.conn.execute(
            f"UPDATE OR IGNORE memory_edges SET source_id = ? WHERE source_id IN ({ph})",
            [merged_id, *gone],
        )
        self.conn.execute(
            f"UPDATE OR IGNORE memory_edges SET target_id = ? WHERE target_id IN ({ph})",
            [merged_id, *gone],
        )

        self.conn.execute(
            "UPDATE memories SET summary = ?, merged_from = ?, updated_at = ?, weight = ?, "
            "consolidation_count = consolidation_count + ? WHERE id = ?",
            (merged, ",".join(gone), now, max_weight, len(gone), merged_id),
        )

        # Remove the redundant originals and their vectors
        self.conn.execute(f"DELETE FROM memories WHERE id IN ({ph})", gone)
        self._drop_vectors(gone)
        # Drop self-edges that may have formed from the re-pointing
        self.conn.execute(
            "DELETE FROM memory_edges WHERE source_id = target_id"
        )
        self.conn.commit()
        return len(gone)

    # ──────────── Cluster detection ────────────

    def find_clusters(
        self,
        *,
        threshold: float = 0.85,
        project: str = None,
        min_size: int = 2,
        sample_limit: int = 500,
    ) -> List[List[Dict]]:
        """Find clusters of semantically near-duplicate memories.

        Pairs of memories whose summary embeddings have cosine similarity at or
        above `threshold` are unioned together. Returns the resulting clusters
        as lists of memory dicts (one list per cluster) with at least
        `min_size` members each, biggest cluster first.

        Only considers memories that have an embedding stored; capped to the
        most recent `sample_limit` rows to keep this O(n²) step bounded.
        """
        query = (
            "SELECT id, topic, summary, weight, project, created_at, embedding "
            "FROM memories WHERE embedding IS NOT NULL "
            "AND (expires_at IS NULL OR expires_at > ?)"
        )
        params: List[Any] = [_utcnow_iso()]
        if project:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(sample_limit)
        rows = self.conn.execute(query, params).fetchall()
        n = len(rows)
        if n < 2:
            return []

        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(n):
            ei = rows[i]["embedding"]
            if not ei:
                continue
            for j in range(i + 1, n):
                ej = rows[j]["embedding"]
                if not ej:
                    continue
                if _cosine_similarity(ei, ej) >= threshold:
                    union(i, j)

        groups: Dict[int, List[Dict]] = {}
        for i, row in enumerate(rows):
            root = find(i)
            d = dict(row)
            d.pop("embedding", None)
            groups.setdefault(root, []).append(d)

        clusters = [g for g in groups.values() if len(g) >= min_size]
        clusters.sort(key=lambda g: (-len(g), min((m["id"] for m in g))))
        return clusters

    def consolidate_clusters(
        self,
        *,
        threshold: float = 0.88,
        project: str = None,
        max_clusters: int = 20,
    ) -> Dict[str, int]:
        """Run find_clusters and consolidate each one.

        For each cluster, keeps the highest-weight memory as the survivor,
        merges all summaries into it, transfers graph edges, deletes the rest.
        Returns {"clusters": N, "merged": K} where K is total rows removed.
        """
        clusters = self.find_clusters(threshold=threshold, project=project)
        merged_total = 0
        processed = 0
        for cluster in clusters[:max_clusters]:
            # Sort survivor candidate to the end (consolidate() keeps rows[-1])
            ranked = sorted(
                cluster,
                key=lambda r: (
                    (r.get("weight") or 0),
                    r.get("created_at") or "",
                ),
            )
            ids = [r["id"] for r in ranked]
            gone = ids[:-1]
            survivor_id = ids[-1]

            # De-duplicate summaries while preserving order
            seen = set()
            parts = []
            for r in ranked:
                s = (r.get("summary") or "").strip()
                if s and s not in seen:
                    seen.add(s)
                    parts.append(s)
            merged_summary = "; ".join(parts)
            max_weight = min(max((r.get("weight") or 1.0) for r in ranked) + 0.2, 3.0)
            now = _utcnow_iso()

            ph = ",".join("?" * len(gone))
            self.conn.execute(
                f"UPDATE OR IGNORE memory_edges SET source_id = ? WHERE source_id IN ({ph})",
                [survivor_id, *gone],
            )
            self.conn.execute(
                f"UPDATE OR IGNORE memory_edges SET target_id = ? WHERE target_id IN ({ph})",
                [survivor_id, *gone],
            )
            self.conn.execute(
                "UPDATE memories SET summary = ?, merged_from = ?, updated_at = ?, weight = ?, "
                "consolidation_count = consolidation_count + ? WHERE id = ?",
                (merged_summary, ",".join(gone), now, max_weight, len(gone), survivor_id),
            )
            self.conn.execute(f"DELETE FROM memories WHERE id IN ({ph})", gone)
            self._drop_vectors(gone)
            merged_total += len(gone)
            processed += 1

        if processed:
            self.conn.execute("DELETE FROM memory_edges WHERE source_id = target_id")
            self.conn.commit()
        return {"clusters": processed, "merged": merged_total}

    def decay_weights(self, decay_rate: float = 0.95):
        """Apply decay to memory weights. Critical/high memories decay slower;
        recently accessed memories are protected from decay this round."""
        # Importance-aware: critical/high decay more gently
        self.conn.execute(
            "UPDATE memories SET weight = weight * CASE "
            "WHEN importance = 'critical' THEN ? "
            "WHEN importance = 'high' THEN ? "
            "ELSE ? END",
            (min(decay_rate + 0.04, 0.999), min(decay_rate + 0.02, 0.999), decay_rate),
        )
        cur = self.conn.execute("SELECT id FROM memories WHERE weight < 0.1")
        dead_ids = [r["id"] for r in cur.fetchall()]
        self.conn.execute("DELETE FROM memories WHERE weight < 0.1")
        self._drop_vectors(dead_ids)
        self._set_meta("last_decay", _utcnow_iso())
        self.conn.commit()

    def maybe_decay(self, interval_hours: float = 24.0, decay_rate: float = 0.95) -> bool:
        """Run decay at most once per interval (lazy, no cron needed).

        Called opportunistically on writes/recalls so weights age automatically.
        Returns True if decay actually ran this call.
        """
        last = self._get_meta("last_decay")
        if last is None:
            # First observation — start the clock, don't decay yet
            self._set_meta("last_decay", _utcnow_iso())
            self.conn.commit()
            return False
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - last_dt < timedelta(hours=interval_hours):
                return False
        except ValueError:
            pass
        self.decay_weights(decay_rate=decay_rate)
        return True

    def _get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM icm_metadata WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO icm_metadata (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ──────────── Project isolation ────────────

    def list_projects(self) -> List[Dict]:
        """Return projects with per-project counts and last activity."""
        rows = self.conn.execute("""
        SELECT project,
               COUNT(*)            AS memory_count,
               MAX(created_at)     AS last_activity,
               AVG(weight)         AS avg_weight
        FROM memories
        WHERE (expires_at IS NULL OR expires_at > ?)
        GROUP BY project
        ORDER BY last_activity DESC
        """, (_utcnow_iso(),)).fetchall()
        out = [dict(r) for r in rows]
        # Layer in memoir counts per project
        memoir_counts = {
            r["project"]: r["c"] for r in self.conn.execute(
                "SELECT project, COUNT(*) AS c FROM memoirs GROUP BY project"
            ).fetchall()
        }
        for r in out:
            r["memoir_count"] = memoir_counts.get(r["project"], 0)
        return out

    def delete_project(self, project: str) -> Dict[str, int]:
        """Delete ALL memories and memoirs for a project. Returns counts."""
        if not project or project == "*":
            raise ValueError("refusing to delete with empty / wildcard project")
        mem_ids = [
            r["id"] for r in self.conn.execute(
                "SELECT id FROM memories WHERE project = ?", (project,)
            ).fetchall()
        ]
        mem_count = len(mem_ids)
        if mem_count:
            placeholders = ",".join("?" * mem_count)
            self.conn.execute(
                f"DELETE FROM memories WHERE id IN ({placeholders})", mem_ids
            )
            self._drop_vectors(mem_ids)
        memoir_cur = self.conn.execute(
            "DELETE FROM memoirs WHERE project = ?", (project,)
        )
        self.conn.commit()
        return {"memories": mem_count, "memoirs": memoir_cur.rowcount}

    def purge_expired(self) -> int:
        """Delete memories whose expires_at has passed. Returns count removed."""
        now = _utcnow_iso()
        cur = self.conn.execute(
            "SELECT id FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        )
        ids = [r["id"] for r in cur.fetchall()]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        self.conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
        self._drop_vectors(ids)
        self.conn.commit()
        log.info(f"Purged {len(ids)} expired memories")
        return len(ids)

    def delete_memory(self, memory_id: str) -> bool:
        """Delete a single memory by ID. Returns True if a row was removed."""
        cur = self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self._drop_vectors([memory_id])
        self.conn.commit()
        return cur.rowcount > 0

    def delete_by_topic(self, topic: str, project: str = None) -> int:
        """Delete all memories matching a topic (optionally scoped to a project)."""
        query = "SELECT id FROM memories WHERE topic = ?"
        params: List[Any] = [topic]
        if project:
            query += " AND project = ?"
            params.append(project)
        ids = [r["id"] for r in self.conn.execute(query, params).fetchall()]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        self.conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
        self._drop_vectors(ids)
        self.conn.commit()
        return len(ids)

    def record_feedback(self, memory_id: str, score: int, reason: str = None) -> bool:
        """Record +1/-1 feedback on a memory and nudge its weight.

        Positive feedback boosts weight (memory survives decay longer);
        negative feedback shrinks it (decay removes it sooner).
        """
        if score not in (-1, 1):
            raise ValueError("score must be -1 or 1")
        exists = self.conn.execute(
            "SELECT 1 FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if not exists:
            return False
        self.conn.execute(
            "INSERT INTO feedback (id, memory_id, score, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (_ulid(), memory_id, score, reason, _utcnow_iso()),
        )
        delta = 0.5 if score == 1 else -0.5
        self.conn.execute(
            "UPDATE memories SET weight = MAX(0.0, MIN(weight + ?, 3.0)) WHERE id = ?",
            (delta, memory_id),
        )
        self.conn.commit()
        return True

    def get_stats(self) -> Dict:
        """Get store statistics."""
        return {
            "memory_count": self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
            "memoir_count": self.conn.execute("SELECT COUNT(*) FROM memoirs").fetchone()[0],
            "concept_count": self.conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0],
            "edge_count": self.conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0],
            "avg_weight": self.conn.execute("SELECT AVG(weight) FROM memories").fetchone()[0],
        }

    def export_all(self) -> Dict:
        """Export all data for backup or migration."""
        tables = {}
        for table in ["memories", "memoirs", "concepts", "concept_links", "memory_edges", "sessions", "feedback"]:
            rows = self.conn.execute(f"SELECT * FROM {table}").fetchall()
            tables[table] = [dict(r) for r in rows]
        return tables

    def import_icm(self, icm_db_path: str) -> int:
        """Import memories from an ICM database."""
        if not os.path.exists(icm_db_path):
            raise FileNotFoundError(f"ICM database not found: {icm_db_path}")

        icm_conn = sqlite3.connect(icm_db_path)
        icm_conn.row_factory = sqlite3.Row

        # Discover which optional columns the source has
        existing_cols = {
            r["name"] for r in icm_conn.execute("PRAGMA table_info(memories)").fetchall()
        }
        wanted = ["id", "created_at", "updated_at", "last_accessed", "access_count",
                  "weight", "topic", "summary", "raw_excerpt", "keywords",
                  "embedding", "source", "project"]
        cols = [c for c in wanted if c in existing_cols]
        if "id" not in cols or "topic" not in cols or "summary" not in cols:
            icm_conn.close()
            raise ValueError("ICM database missing required columns (id, topic, summary)")

        col_list = ", ".join(cols)
        placeholders = ", ".join("?" * len(cols))
        rows = icm_conn.execute(
            f"SELECT {col_list} FROM memories ORDER BY created_at"
        ).fetchall()

        count = 0
        for row in rows:
            try:
                self.conn.execute(
                    f"INSERT OR IGNORE INTO memories ({col_list}) VALUES ({placeholders})",
                    tuple(row[c] for c in cols),
                )
                count += 1
            except Exception as e:
                log.warning(f"Import skipped for {row['id']}: {e}")

        self.conn.commit()
        icm_conn.close()
        log.info(f"Imported {count} memories from ICM")
        return count

    # ──────────── Helpers ────────────

    def _find_duplicate(self, topic: str, summary: str, project: str) -> str | None:
        """Check if a similar memory already exists. Returns ID or None."""
        # Normalize for fuzzy matching: lowercase, strip punctuation
        norm_summary = re.sub(r'[^\w\s]', '', summary.lower().strip())
        if len(norm_summary) < 15:
            return None

        # Stop words to filter for better similarity
        stop_words = {'the', 'a', 'an', 'is', 'was', 'are', 'were', 'been',
                      'has', 'have', 'had', 'to', 'for', 'of', 'in', 'on', 'at',
                      'by', 'with', 'from', 'this', 'that', 'it', 'and', 'or',
                      'now', 'then', 'just', 'also', 'still', 'already', 'not'}

        # Find memories with same topic
        candidates = self.conn.execute("""
        SELECT id, summary FROM memories
        WHERE topic = ? AND project = ?
        ORDER BY created_at DESC LIMIT 5
        """, (topic, project)).fetchall()

        words_new = set(norm_summary.split()) - stop_words
        if len(words_new) < 3:
            return None

        best_sim = 0
        best_id = None
        for row in candidates:
            existing = re.sub(r'[^\w\s]', '', row["summary"].lower().strip())
            words_existing = set(existing.split()) - stop_words
            if not words_new or not words_existing:
                continue
            overlap = len(words_new & words_existing)
            union = len(words_new | words_existing)
            similarity = overlap / union if union > 0 else 0

            # Track best match (40%+ with stop words filtered)
            if similarity >= 0.40 and similarity > best_sim:
                best_sim = similarity
                best_id = row["id"]

        return best_id

    def _store_vector(self, memory_id: str, embedding: bytes):
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO vec_memories (memory_id, embedding) VALUES (?, ?)",
                (memory_id, embedding)
            )
        except Exception:
            pass

    def _drop_vectors(self, memory_ids: List[str]):
        """Remove vec0 rows for deleted memories (no-op if vec not enabled)."""
        if not self.vec_enabled or not memory_ids:
            return
        try:
            placeholders = ",".join("?" * len(memory_ids))
            self.conn.execute(
                f"DELETE FROM vec_memories WHERE memory_id IN ({placeholders})",
                list(memory_ids),
            )
        except Exception:
            pass

    def _touch(self, memory_ids: List[str]):
        if not memory_ids:
            return
        now = _utcnow_iso()
        placeholders = ",".join("?" * len(memory_ids))
        self.conn.execute(
            f"UPDATE memories SET last_accessed = ?, access_count = access_count + 1 "
            f"WHERE id IN ({placeholders})",
            [now, *memory_ids],
        )
        self.conn.commit()


# ──────────── Utilities ────────────

def _ulid() -> str:
    """Generate a ULID-like ID (time-sortable)."""
    import random, string
    ts = int(time.time() * 1000)
    ts_enc = _encode_ts(ts)
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))
    return f"{ts_enc}{rand}"

def _encode_ts(ts: int) -> str:
    """Crockford Base32 encode a timestamp."""
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    result = []
    for _ in range(10):
        result.append(alphabet[ts & 0x1F])
        ts >>= 5
    return ''.join(reversed(result))

def _hash_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]

def _cosine_similarity(vec_a: bytes, vec_b: bytes) -> float:
    """Compute cosine similarity between two float32 byte vectors."""
    import struct
    size = min(len(vec_a), len(vec_b)) // 4
    a = struct.unpack(f'{size}f', vec_a[:size * 4])
    b = struct.unpack(f'{size}f', vec_b[:size * 4])
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)
