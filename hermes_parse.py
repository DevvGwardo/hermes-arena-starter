"""
Hermes-Arena tolerant JSON parser.

Some LLMs — especially smaller / quantized / locally-hosted ones — emit JSON
that's almost valid but not quite: trailing commas, single quotes, unquoted
keys, Python-style None/True/False, ```json fences, prose padding, hard
truncation when max_tokens hits mid-array. The default `json.loads(text)`
trips on every one of these, and the bot ends up holding when the model
actually produced a perfectly serviceable response.

This module layers three forgiving stages so the bot keeps trading:

  1. Raw `json.loads(text.strip())` — when the model emits clean JSON,
     this short-circuits before any extraction work.
  2. `extract_json_object` — strips ```fences``` and isolates the first
     balanced `{...}` or `[...]` when the model padded with prose.
  3. `json-repair` — fixes the long tail (trailing commas, unquoted keys,
     single/smart quotes, Python literals, truncation, comments). Optional
     dependency; if it isn't installed, stages 1+2 still run and the
     parser logs a clear warning the first time stage 3 would have helped.

This is structurally identical to the layered parser in the yetifi backend's
`services/hermesProvider.ts`, so behavior is consistent across the server's
built-in traders and any user agent that imports this module.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

log = logging.getLogger("hermes-parse")

# Lazy-import json-repair so a participant who upgrades agent.py before
# re-running `pip install -r requirements.txt` still gets stages 1 and 2.
try:
    from json_repair import repair_json as _json_repair_str  # type: ignore
except ImportError:  # pragma: no cover
    _json_repair_str = None


def extract_json_object(text: str) -> str:
    """Strip ```fences``` and isolate the first balanced JSON expression.

    Balances both `{...}` and `[...]`, picking whichever opener appears
    first. Models that wrap output in ```json ...``` or pad with prose
    ("Here is the JSON: {...} hope this helps") still parse.
    """
    s = text.strip()
    if s.startswith("```"):
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", s, re.IGNORECASE)
        if m:
            return m.group(1).strip()

    brace_idx = s.find("{")
    bracket_idx = s.find("[")
    if brace_idx == -1 and bracket_idx == -1:
        return s
    if brace_idx != -1 and (bracket_idx == -1 or brace_idx < bracket_idx):
        start, opener, closer = brace_idx, "{", "}"
    else:
        start, opener, closer = bracket_idx, "[", "]"

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return s[start:i + 1].strip()
    return s


def safe_json_parse(text: str) -> Optional[Any]:
    """Three-stage tolerant parse. Returns None if every stage fails.

    Returns the parsed value (dict, list, str, etc.) on success — callers
    are responsible for shape-validating the result. Designed to be a
    drop-in replacement for `json.loads(text)` in chat-completion result
    handling, where the response is *almost* always JSON but every model
    has its own way of fraying around the edges.
    """
    if not text:
        return None

    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, TypeError):
        pass

    try:
        return json.loads(extract_json_object(text))
    except (json.JSONDecodeError, TypeError):
        pass

    if _json_repair_str is None:
        log.warning(
            "[hermes-parse] JSON parse failed and json-repair is not installed; "
            "run `pip install -r requirements.txt` to enable repair. sample=%r",
            text[:300],
        )
        return None

    try:
        repaired = _json_repair_str(text)
    except (ValueError, TypeError) as exc:
        log.warning("[hermes-parse] json-repair raised: %s; sample=%r", exc, text[:300])
        return None

    try:
        return json.loads(repaired)
    except (json.JSONDecodeError, TypeError):
        log.warning("[hermes-parse] every stage failed; sample=%r", text[:300])
        return None
