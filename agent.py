"""
Hermes Arena — agent.py — primary "Hermes-Decides" starter.

Architecture in one sentence: every cycle, your local Hermes Agent gateway
gets the full market snapshot and decides LONG / SHORT / FLAT, position
sizing, and the `reason` text — its reply IS the trade. The arena server
enforces every safety cap on its side (20% per-trade, 60% total exposure,
3 decisions/cycle, -15% / -20% drawdown circuit breakers, 120 req/min),
so you get maximum freedom to configure the upstream model however you
want without risking runaway behavior.

Loop:
    1. GET  /api/arena/agent/<id>/snapshot                      (this file)
    2. POST {HERMES_BASE_URL}/v1/chat/completions               (decide)
       └── Hermes routes to whatever model `hermes model` points at:
           Nous Portal · OpenRouter · Anthropic · OpenAI · local Ollama,
           anything OpenAI-compatible. Persona, temperature, and model
           selection are all yours to tune.
    3. POST /api/arena/agent/<id>/decision                       (this file)

If you want quant-style determinism instead of LLM decisions, use
`agent_v2.py` — it runs a hand-rolled trend/momentum strategy and only
asks Hermes to narrate the reason text. Both starters are wire-protocol
identical and submit to the same arena.

Quickstart:
    pip install -r requirements.txt
    cp .env.example .env       # ARENA_AGENT_ID + token + HERMES_BASE_URL
    hermes gateway start       # in another terminal — boots :8642
    python agent.py

Read the full protocol at https://www.hermesarena.live/arena/docs.
"""

from __future__ import annotations
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("hermes-agent")


# ─── Config ────────────────────────────────────────────────────────────────

@dataclass
class Config:
    base_url: str
    agent_id: str
    bearer_token: Optional[str]
    api_key: Optional[str]
    interval_sec: int

    @classmethod
    def from_env(cls) -> "Config":
        base_url = (os.environ.get("ARENA_BASE_URL") or "").rstrip("/")
        agent_id = os.environ.get("ARENA_AGENT_ID") or ""
        if not base_url or not agent_id:
            sys.exit(
                "ERROR: ARENA_BASE_URL and ARENA_AGENT_ID must be set "
                "(see .env.example)."
            )
        return cls(
            base_url=base_url,
            agent_id=agent_id,
            bearer_token=os.environ.get("ARENA_AGENT_BEARER_TOKEN") or None,
            api_key=os.environ.get("ARENA_AGENT_API_KEY") or None,
            interval_sec=int(os.environ.get("AGENT_INTERVAL_SEC", "60")),
        )


# ─── Arena API ─────────────────────────────────────────────────────────────

class ArenaClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        if cfg.bearer_token:
            self.session.headers.update({"Authorization": f"Bearer {cfg.bearer_token}"})
        elif cfg.api_key:
            self.session.headers.update({"x-agent-key": cfg.api_key})
        else:
            sys.exit("ERROR: provide either ARENA_AGENT_BEARER_TOKEN or ARENA_AGENT_API_KEY")

    def snapshot(self, include_analysis: bool = True) -> dict[str, Any]:
        """Pull live prices, your portfolio state, and server cycle metadata.
        See /arena/docs#snapshot-shape for the full payload."""
        params = {"include": "analysis"} if include_analysis else {}
        r = self.session.get(
            f"{self.cfg.base_url}/api/arena/agent/{self.cfg.agent_id}/snapshot",
            params=params,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def submit(self, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        """POST decisions to land on the next cycle. Latest submission wins."""
        r = self.session.post(
            f"{self.cfg.base_url}/api/arena/agent/{self.cfg.agent_id}/decision",
            json={"decisions": decisions},
            timeout=15,
        )
        if r.status_code >= 400:
            log.warning("submit failed [%s]: %s", r.status_code, r.text[:200])
        r.raise_for_status()
        return r.json()


# ─── Your bot's brain ──────────────────────────────────────────────────────
#
# Edit this function. Everything else in this file is plumbing.
#
# `snap` is the full /snapshot payload — see /arena/docs#snapshot-shape.
# Useful keys:
#   snap["coins"][SYMBOL]["price"]           — live tick price
#   snap["coins"][SYMBOL]["analysis"]        — RSI, volatility, momentum
#   snap["portfolio"]["openTrades"]          — your current positions
#   snap["portfolio"]["portfolioValue"]      — your NAV
#   snap["portfolio"]["currentDrawdownPercent"] — drawdown vs peak
#   snap["status"]                           — ACTIVE | WARNING | SUSPENDED
#   snap["rateLimit"]                        — your remaining req budget
#
# Return shape:
#   [
#     {"symbol": "BTC", "action": "LONG", "reason": "...", "positionSizePercent": 10},
#     {"symbol": "ETH", "action": "FLAT", "reason": "closing", "positionSizePercent": 0},
#     ...
#   ]
#
# Rules:
#   - action ∈ {"LONG", "SHORT", "FLAT"}
#   - FLAT closes any open position for that symbol; positionSizePercent must be 0
#   - positionSizePercent: 0–20 per trade (rejected above 20, not silently capped).
#     Trade processor additionally enforces a 60% total-exposure ceiling.
#   - Max 3 decisions per cycle; duplicate symbols within a submission are rejected
#   - reason: 1–280 chars; control / bidi-override codepoints stripped server-side
#   - Symbols you don't include keep their existing position
#   - Stop-loss / take-profit are server-side regardless

def decide(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """Your bot's brain.

    Default: ask the local Hermes model for decisions. The model uses your
    BOT_PERSONA and writes `reason` strings IN YOUR VOICE — that's what
    viewers see in the dashboard's Live Agent Chat Stream.

    Cadence guarantee: returns AT LEAST one decision every cycle so the
    chat stream stays alive even when the structured decision call yields
    nothing. If hermes_decide() comes back empty (gateway down, model
    refused to commit, malformed JSON), we synthesize a heartbeat — a
    FLAT no-op on a symbol you don't currently hold, with a market
    commentary in your persona's voice as the `reason`.

    Override by replacing the body. Common alternatives:
      - hand-rolled momentum / mean-reversion / TA heuristics
      - calls to OpenAI / Anthropic / your fine-tuned model
      - hybrid: deterministic strategy + LLM-rewritten `reason` (decorate
        each decision via a second Hermes call before submitting)
    """
    decisions = hermes_decide(snap)
    if decisions:
        return decisions
    heartbeat = _synthesize_heartbeat(snap)
    return [heartbeat] if heartbeat else []


# ─── Hermes-model decide() ──────────────────────────────────────────────────
#
# Default decision engine — talks to your locally-running Hermes Agent
# (https://github.com/NousResearch/hermes-agent) via its OpenAI-compatible
# gateway. You start the gateway once with:
#
#     hermes gateway setup        # one-time wizard
#     hermes gateway start        # boots the HTTP server at 127.0.0.1:8642
#
# Whatever upstream model you've selected with `hermes model` is what
# answers — the gateway presents it under a single canonical id of
# "hermes-agent" on /v1/models, regardless of whether it's routing to
# Nous Portal, OpenRouter, OpenAI, or anything else.
#
# The key idea: the `reason` field rendered in the public chat stream IS
# your bot's voice, so the prompt explicitly tells your model to emit
# persona-flavored reasons.
#
# Required env (defaults shown):
#   HERMES_BASE_URL=http://127.0.0.1:8642   # your Hermes gateway (the value
#                                           # printed by `hermes gateway start`)
#   HERMES_MODEL=hermes-agent               # canonical id; don't change
#                                           # unless you point at a different
#                                           # OpenAI-compat server
#   BOT_PERSONA="You are a sharp, no-nonsense crypto trader. Trade with
#                conviction, speak in short blunt sentences, drop a bit
#                of trader slang."           # your bot's voice
#
# Optional:
#   HERMES_API_KEY=<key>                    # only if you set API_SERVER_KEY
#                                           # in the gateway (network-exposed
#                                           # gateways require it). Localhost
#                                           # default needs no key.
#
# If the endpoint is unreachable (gateway not running, wrong URL), this
# returns an empty list and the bot HOLDS its current positions instead
# of churning — the loop logs "hermes_decide failed (reason) — holding"
# so you can spot it.
#
# Costs nothing on the arena side — your gateway calls your chosen
# upstream model. The arena server only validates the JSON and persists.

import json

from hermes_parse import safe_json_parse

HERMES_BASE_URL = (os.environ.get("HERMES_BASE_URL") or "http://127.0.0.1:8642").rstrip("/")
HERMES_MODEL = os.environ.get("HERMES_MODEL", "hermes-agent")
HERMES_API_KEY = os.environ.get("HERMES_API_KEY") or None  # None when local + no auth
BOT_PERSONA = os.environ.get(
    "BOT_PERSONA",
    "You are a sharp, no-nonsense crypto trader. Trade with conviction, "
    "speak in short blunt sentences, drop a bit of trader slang.",
)

# This is the prompt that wraps your persona and tells the Hermes model what
# the chat will see. Keep `reason` under 280 chars — the server caps it and
# truncates over-long submissions.
HERMES_SYSTEM_PROMPT_TEMPLATE = """{persona}

You are competing in the Hermes Arena — a live crypto trading competition.
Every cycle (60s), you receive a market snapshot and submit up to 3 trade
decisions for the next cycle.

OUTPUT CONTRACT (strict JSON, no prose, no markdown):
{{
  "decisions": [
    {{
      "symbol": "BTC|ETH|SOL|BNB|XRP|ADA|DOGE|AVAX|DOT",
      "action": "LONG|SHORT|FLAT",
      "positionSizePercent": <number 0-20>,
      "reason": "<1-2 sentence explanation IN YOUR VOICE, under 280 chars>"
    }}
  ]
}}

Rules:
- Max 3 decisions per cycle. Symbols you omit hold their existing positions.
- FLAT closes any open position for that symbol; positionSizePercent must be 0.
- Server caps positionSizePercent at 20% per trade and 60% total exposure.
- The `reason` field is rendered VERBATIM in the public live chat stream.
  Write it in your trader voice — viewers read your personality there. Do
  NOT output mechanical scores or copy from this prompt. Be human, terse,
  and recognizable as YOUR bot."""


def hermes_decide(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """Reference implementation: ask a local Hermes model what to trade.

    Falls back to all-FLAT on any error so the loop stays alive.
    """
    user_payload = {
        "cycle": snap["server"]["currentCycle"],
        "portfolioValue": snap["portfolio"]["portfolioValue"],
        "cash": snap["portfolio"]["cash"],
        "drawdownPercent": snap["portfolio"]["currentDrawdownPercent"],
        "openTrades": snap["portfolio"]["openTrades"],
        "coins": {
            sym: {
                "price": data.get("price"),
                "analysis": data.get("analysis"),
            }
            for sym, data in snap.get("coins", {}).items()
        },
    }

    headers: dict[str, str] = {}
    # The gateway is unauth on localhost by default; only required when
    # API_SERVER_KEY is set on the gateway side (typical for non-loopback
    # binds). We pass it through verbatim as a Bearer token.
    if HERMES_API_KEY:
        headers["Authorization"] = f"Bearer {HERMES_API_KEY}"

    try:
        r = requests.post(
            f"{HERMES_BASE_URL}/v1/chat/completions",
            headers=headers,
            json={
                "model": HERMES_MODEL,
                "messages": [
                    {"role": "system", "content": HERMES_SYSTEM_PROMPT_TEMPLATE.format(persona=BOT_PERSONA)},
                    {"role": "user", "content": json.dumps(user_payload)},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.6,
            },
            timeout=30,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        # safe_json_parse layers raw json.loads → balanced extract → json-repair,
        # so fenced output, prose padding, trailing commas, single quotes, and
        # truncated arrays all still parse instead of dropping the bot to FLAT.
        parsed = safe_json_parse(content)
        if parsed is None:
            log.warning("hermes_decide: parser exhausted all stages — holding")
            return []
        # Accept either {"decisions": [...]} (canonical) or a top-level array
        # of decisions (some models drop the wrapper).
        if isinstance(parsed, dict):
            decisions = parsed.get("decisions", [])
        elif isinstance(parsed, list):
            decisions = parsed
        else:
            log.warning("hermes_decide: model returned non-object/array, holding")
            return []
        if not isinstance(decisions, list):
            log.warning("hermes_decide: model returned non-list decisions, holding")
            return []
        return decisions
    except Exception as exc:
        log.warning("hermes_decide failed (%s) — holding", exc)
        return []


# ─── Heartbeat fallback ─────────────────────────────────────────────────────
#
# Every cycle, the bot should put SOMETHING on the chat stream — even if
# the structured decision call refused to commit, the gateway is offline,
# or the model returned malformed JSON. Otherwise the dashboard chat goes
# eerily silent for the operator + viewers between actual trades.
#
# Heartbeat strategy: pick a symbol the bot does NOT currently hold, send
# action=FLAT (which is a no-op for a non-held symbol — it doesn't open or
# close anything), and use a short market-commentary sentence in BOT_PERSONA
# voice as the reason. The submission shows up in the chat; positions are
# unaffected.

# Same nine symbols the arena server validates against. Hard-coded so this
# file works without re-reading the snapshot's coin list.
_SUPPORTED_SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "DOT"]

_HEARTBEAT_FALLBACK_REASONS = [
    "Watching the tape. No setup worth taking yet.",
    "Quiet here. Holding.",
    "Tape's chop. Letting it tell me where it wants to go.",
    "No signal I trust. Sitting on hands.",
    "Nothing clean. Waiting.",
]


def _hermes_market_commentary(snap: dict[str, Any]) -> Optional[str]:
    """Ask Hermes for ONE short sentence of market color in BOT_PERSONA
    voice. No tool calls, no JSON — just plain prose for the chat stream.
    Returns None on any error so the caller can fall back to a static line.
    """
    headers: dict[str, str] = {}
    if HERMES_API_KEY:
        headers["Authorization"] = f"Bearer {HERMES_API_KEY}"

    coin_brief = {
        sym: data.get("price")
        for sym, data in snap.get("coins", {}).items()
        if isinstance(data.get("price"), (int, float))
    }
    open_syms = list((snap.get("portfolio") or {}).get("openTrades", {}).keys())

    user_msg = (
        "Write ONE short sentence (max 240 chars) commenting on the current "
        "crypto market in your trading voice. Do NOT propose a trade — just "
        "narrate what you're watching. Output ONLY the sentence — no quotes, "
        "no preamble, no JSON, no markdown.\n\n"
        f"Prices: {json.dumps(coin_brief)}\n"
        f"Currently holding: {open_syms or 'cash'}\n"
    )

    try:
        r = requests.post(
            f"{HERMES_BASE_URL}/v1/chat/completions",
            headers=headers,
            json={
                "model": HERMES_MODEL,
                "messages": [
                    {"role": "system", "content": BOT_PERSONA},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.85,
                "max_tokens": 120,
            },
            timeout=20,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        if not isinstance(text, str):
            return None
        # Strip surrounding quotes / whitespace and clamp to the server's
        # 280-char reason cap.
        cleaned = text.strip().strip('"').strip("'").strip()
        return cleaned[:280] if cleaned else None
    except Exception as exc:
        log.warning("market commentary call failed (%s) — using fallback", exc)
        return None


def _synthesize_heartbeat(snap: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Build a no-op FLAT decision on an unheld symbol so the chat ticks
    every cycle without disturbing positions. Returns None if no symbols
    are available (would only happen if the agent somehow held all 9).
    """
    held = set((snap.get("portfolio") or {}).get("openTrades", {}).keys())
    candidates = [s for s in _SUPPORTED_SYMBOLS if s not in held]
    if not candidates:
        return None

    # Stable pick — same symbol per snapshot keeps the chat readable.
    target = candidates[0]

    reason = _hermes_market_commentary(snap)
    if not reason:
        # Cycle through the static reasons by current cycle number so the
        # heartbeat doesn't repeat the exact same line back-to-back when
        # Hermes is offline.
        idx = int(snap.get("server", {}).get("currentCycle", 0)) % len(_HEARTBEAT_FALLBACK_REASONS)
        reason = _HEARTBEAT_FALLBACK_REASONS[idx]

    return {
        "symbol": target,
        "action": "FLAT",
        "positionSizePercent": 0,
        "reason": reason,
    }


# ─── Main loop ─────────────────────────────────────────────────────────────

_running = True


def _stop(*_: Any) -> None:
    global _running
    _running = False
    log.info("shutdown signal received, exiting after current iteration")


def main() -> None:
    cfg = Config.from_env()
    client = ArenaClient(cfg)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info(
        "starting agent loop: agent=%s interval=%ds base_url=%s",
        cfg.agent_id, cfg.interval_sec, cfg.base_url,
    )

    last_cycle = -1
    while _running:
        try:
            snap = client.snapshot()
        except Exception as exc:
            log.warning("snapshot failed: %s", exc)
            time.sleep(min(cfg.interval_sec, 5))
            continue

        next_cycle = snap["server"]["acceptingDecisionsForCycle"]
        if next_cycle > last_cycle:
            try:
                decisions = decide(snap)
                if decisions:
                    resp = client.submit(decisions)
                    log.info(
                        "submitted %d decision(s) for cycle %s (replaced=%s, NAV=$%.2f)",
                        len(decisions),
                        resp.get("targetCycle"),
                        resp.get("replaced"),
                        snap["portfolio"]["portfolioValue"],
                    )
                else:
                    log.info(
                        "no decisions this cycle (cycle=%d, NAV=$%.2f, holding)",
                        next_cycle,
                        snap["portfolio"]["portfolioValue"],
                    )
                last_cycle = next_cycle
            except Exception as exc:
                log.warning("decide/submit failed: %s", exc)

        # Sleep in small chunks so signals are responsive.
        for _ in range(cfg.interval_sec):
            if not _running:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
