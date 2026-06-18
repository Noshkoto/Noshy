"""
Noshy extractor — LLM-powered fact extraction from conversation transcripts.
Uses the Hermes agent (or any OpenAI-compatible API) to extract structured
memories, keywords, and relationships from raw text.
"""
import os
import json
import time
import logging
import hashlib
from typing import List, Dict, Optional, Any
from datetime import datetime

log = logging.getLogger("aion.extract")

EXTRACTION_PROMPT = """You are a memory extraction engine. Extract structured facts from this conversation transcript.

Output ONLY valid JSON — no markdown, no commentary. Use this exact structure:

{
  "memories": [
    {
      "topic": "short-topic-slug",
      "summary": "one-sentence factual summary",
      "importance": "critical|high|medium|low",
      "keywords": ["keyword1", "keyword2"],
      "raw_excerpt": "verbatim quote from transcript (max 200 chars)"
    }
  ],
  "concepts": ["concept-name-1", "concept-name-2"],
  "relationships": [
    {"from_memory_index": 0, "to_memory_index": 1, "relation": "contradicts|extends|depends_on|answers|caused_by"}
  ]
}

Importance scoring rules:
- critical: Security vulnerabilities, data loss, breaking changes, production incidents
- high: Bug fixes, architectural decisions, config changes, deployment changes, performance fixes
- medium: Feature additions, refactoring, tool changes, documentation updates, useful discoveries
- low: Minor tweaks, cosmetic changes, speculative ideas, general discussion

Rules:
- Extract facts, decisions, preferences, bugs, fixes, and knowledge gained
- Skip small talk, greetings, and obvious filler
- Max 8 memories per extraction
- topic must be kebab-case, max 40 chars
- Use CONTEXT from the transcript, don't invent facts

Transcript:
{transcript}

JSON output:"""

IMPORTANCE_PROMPT = """You are a memory importance classifier. Read the memory below and decide how important it is to remember in future sessions.

Memory topic: {topic}
Memory summary: {summary}

Rules:
- critical: Security vulnerabilities, data loss, breaking changes, production incidents, irreversible decisions
- high: Bug fixes, architectural decisions, config changes, deployment changes, performance fixes, user preferences
- medium: Feature additions, refactoring, tool changes, documentation updates, useful discoveries
- low: Minor tweaks, cosmetic changes, speculative ideas, general discussion

Output ONLY one word from this set: critical, high, medium, low. No punctuation."""


CONSOLIDATION_PROMPT = """You are a memory consolidation engine. Given multiple related memories, merge them into a single consolidated fact.

Input memories (JSON array):
{memories}

Output ONLY valid JSON:
{{
  "merged_summary": "consolidated summary combining all facts",
  "merged_topic": "unified-topic-slug",
  "resolved_contradictions": "explain any contradictions and how you resolved them",
  "confidence": 0.0-1.0
}}"""


def extract_facts(
    transcript: str,
    *,
    api_base: str = None,
    api_key: str = None,
    model: str = None,
    max_memories: int = 8,
) -> List[Dict]:
    """Extract memories from a transcript using an LLM.

    Args:
        transcript: Raw conversation text
        api_base: OpenAI-compatible API base URL (default: use Hermes gateway)
        api_key: API key
        model: Model name
        max_memories: Max memories to extract

    Returns list of memory dicts and concepts/relationships
    """
    if len(transcript.strip()) < 50:
        return []

    # str.replace, not str.format — the prompt contains literal { and } in the
    # JSON example, so format() would raise KeyError on those braces.
    prompt = EXTRACTION_PROMPT.replace("{transcript}", transcript[:12000])

    response = _call_llm(prompt, api_base=api_base, api_key=api_key, model=model)
    if not response:
        return []

    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        # Try to extract JSON from the response
        import re
        match = re.search(r'\{[\s\S]*\}', response)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                log.warning("Failed to parse extraction JSON")
                return []
        else:
            log.warning("No JSON found in extraction response")
            return []

    results = []
    memo_index = {}

    for i, mem in enumerate(data.get("memories", [])[:max_memories]):
        topic = mem.get("topic", "general")
        summary = mem.get("summary", "")
        importance = mem.get("importance", "medium")
        keywords = mem.get("keywords", [])
        raw = mem.get("raw_excerpt", "")

        if len(summary) < 10:
            continue

        # Create memory ID from content hash
        content = f"{topic}:{summary}"
        memory_id = hashlib.sha256(content.encode()).hexdigest()[:24]

        results.append({
            "id": memory_id,
            "topic": topic,
            "summary": summary,
            "importance": importance,
            "keywords": keywords,
            "raw_excerpt": raw,
            "source": "llm-extract",
        })
        memo_index[i] = memory_id

    # Generate relationships
    for rel in data.get("relationships", []):
        from_idx = rel.get("from_memory_index")
        to_idx = rel.get("to_memory_index")
        if from_idx in memo_index and to_idx in memo_index:
            results.append({
                "_type": "relationship",
                "source_id": memo_index[from_idx],
                "target_id": memo_index[to_idx],
                "relation": rel.get("relation", "related"),
            })

    # Generate concepts
    for concept in data.get("concepts", []):
        results.append({
            "_type": "concept",
            "name": concept,
        })

    return results


_VALID_IMPORTANCE = {"critical", "high", "medium", "low"}


CONTRADICTION_PROMPT = """You are a memory contradiction checker. Two memory
summaries are below. Decide whether they assert conflicting facts about the
same subject (e.g. opposite preferences, mutually exclusive choices, replaced
decisions). Disagreements in tone or emphasis do NOT count — only factual
conflicts.

Memory A: {summary_a}
Memory B: {summary_b}

Output ONLY valid JSON, no commentary:
{{
  "contradicts": true|false,
  "confidence": 0.0-1.0,
  "explanation": "one short sentence — what actually conflicts, or '' if not"
}}"""


_CONTRADICTION_FAIL = {"contradicts": False, "confidence": 0.0, "explanation": "unavailable"}


def check_contradiction(
    summary_a: str,
    summary_b: str,
    *,
    api_base: str = None,
    api_key: str = None,
    model: str = None,
) -> dict:
    """Ask the LLM whether two memory summaries assert conflicting facts.

    Returns {"contradicts": bool, "confidence": float (0-1), "explanation": str}.
    On any failure (no endpoint configured, API error, bad JSON) returns the
    "unavailable" default rather than raising — callers should never have to
    special-case this.
    """
    if not summary_a or not summary_b:
        return dict(_CONTRADICTION_FAIL)
    a = summary_a.strip()[:400]
    b = summary_b.strip()[:400]
    if len(a) < 5 or len(b) < 5:
        return dict(_CONTRADICTION_FAIL)
    prompt = CONTRADICTION_PROMPT.format(summary_a=a, summary_b=b)
    response = _call_llm(
        prompt, api_base=api_base, api_key=api_key, model=model,
        max_tokens=120, temperature=0.0,
    )
    if not response:
        return dict(_CONTRADICTION_FAIL)
    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        import re
        m = re.search(r"\{[\s\S]*\}", response)
        if not m:
            return dict(_CONTRADICTION_FAIL)
        try:
            data = json.loads(m.group())
        except json.JSONDecodeError:
            return dict(_CONTRADICTION_FAIL)
    try:
        contradicts = bool(data.get("contradicts", False))
        confidence = float(data.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
        explanation = str(data.get("explanation", "") or "")[:240]
    except (TypeError, ValueError):
        return dict(_CONTRADICTION_FAIL)
    return {"contradicts": contradicts, "confidence": confidence, "explanation": explanation}


def predict_importance(
    topic: str,
    summary: str,
    *,
    api_base: str = None,
    api_key: str = None,
    model: str = None,
    default: str = "medium",
) -> str:
    """Ask the LLM to classify a memory's importance.

    Returns one of: critical, high, medium, low. Falls back to `default` on
    any error (no API key, parse failure, network error) so callers can use
    this as a best-effort enhancement.
    """
    if not summary or len(summary.strip()) < 5:
        return default
    prompt = IMPORTANCE_PROMPT.format(topic=topic or "(no topic)", summary=summary)
    response = _call_llm(
        prompt, api_base=api_base, api_key=api_key, model=model,
        max_tokens=8, temperature=0.0,
    )
    if not response:
        return default
    word = response.strip().lower().split()[0].strip(".,'\"")
    if word in _VALID_IMPORTANCE:
        return word
    return default


def stream_extract(
    transcript_chunks,
    *,
    api_base: str = None,
    api_key: str = None,
    model: str = None,
    max_memories_per_chunk: int = 4,
    chunk_overlap: int = 200,
):
    """Generator: extract memories incrementally as transcript chunks arrive.

    Yields lists of memory dicts per processed chunk. Buffers a short tail to
    keep context continuity across chunk boundaries. Use this for long-running
    sessions where you want extraction to happen as work proceeds, rather than
    waiting for the entire transcript to finish.

    `transcript_chunks` can be any iterable of strings (streamed lines, gathered
    tool outputs, etc.).
    """
    buf = ""
    flush_at = 1500
    for chunk in transcript_chunks:
        if not chunk:
            continue
        buf += chunk
        if len(buf) < flush_at:
            continue
        facts = extract_facts(
            buf, api_base=api_base, api_key=api_key, model=model,
            max_memories=max_memories_per_chunk,
        )
        if facts:
            yield facts
        # Keep a small tail so the next chunk has continuity
        buf = buf[-chunk_overlap:] if chunk_overlap > 0 else ""
    if buf.strip():
        facts = extract_facts(
            buf, api_base=api_base, api_key=api_key, model=model,
            max_memories=max_memories_per_chunk,
        )
        if facts:
            yield facts


def consolidate_memories(memories: List[Dict], *, api_base: str = None, api_key: str = None, model: str = None) -> Dict:
    """Merge multiple memories on the same topic into one."""
    if len(memories) < 2:
        return None

    prompt = CONSOLIDATION_PROMPT.format(
        memories=json.dumps([{
            "topic": m.get("topic"),
            "summary": m.get("summary"),
            "importance": m.get("importance"),
        } for m in memories], indent=2)
    )

    response = _call_llm(prompt, api_base=api_base, api_key=api_key, model=model)
    if not response:
        return None

    try:
        return json.loads(response)
    except json.JSONDecodeError:
        import re
        match = re.search(r'\{[\s\S]*\}', response)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                log.warning("JSON extraction fallback also failed to parse")
    return None


def _call_llm(prompt: str, *, api_base: str = None, api_key: str = None,
              model: str = None, max_tokens: int = 2000,
              temperature: float = 0.1) -> str:
    """Call an LLM via OpenAI-compatible API."""
    import urllib.request, urllib.error

    if api_base is None or api_key is None or model is None:
        try:
            from config import get as _cfg
            cfg_base = _cfg("api_base"); cfg_key = _cfg("api_key"); cfg_model = _cfg("model")
        except Exception:
            cfg_base = cfg_key = cfg_model = None
        if api_base is None:
            api_base = cfg_base or os.environ.get("NOSHY_API_BASE", "http://127.0.0.1:8642/v1")
        if api_key is None:
            api_key = cfg_key or os.environ.get("NOSHY_API_KEY",
                                                os.environ.get("API_SERVER_KEY", ""))
        if model is None:
            model = cfg_model or os.environ.get("NOSHY_MODEL", "hermes-agent")

    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a precise fact-extraction engine. Output ONLY valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Retry with exponential backoff on transient failures
    import time as _time
    max_retries = 3
    for attempt in range(max_retries):
        req = urllib.request.Request(
            f"{api_base}/chat/completions",
            data=body,
            headers=headers,
        )
        try:
            resp = urllib.request.urlopen(req, timeout=60)
            data = json.loads(resp.read())
            return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < max_retries - 1:
                wait = 2 ** attempt
                log.warning(f"LLM call got HTTP {e.code}, retrying in {wait}s (attempt {attempt+1}/{max_retries})")
                _time.sleep(wait)
                continue
            log.error(f"LLM call failed: HTTP {{e.code}}")
            return ""
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                log.warning(f"LLM call failed: {e}, retrying in {wait}s")
                _time.sleep(wait)
                continue
            log.error(f"LLM call failed after {max_retries} attempts: {e}")
            return ""
    return ""
