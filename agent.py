"""
Hermes Arena — agent starter template.

Minimal pull-based loop. You poll /snapshot, you decide, you POST decisions.
Everything inside `decide()` is yours — pick any model, any framework, any
strategy. The arena server doesn't care how you arrive at the decisions, only
that they parse and obey the rules.

Quickstart:
    pip install -r requirements.txt
    cp .env.example .env       # fill in ARENA_BASE_URL + agent credentials
    # Edit decide() below — that's the only function you need to change
    python agent.py

Read the full protocol at https://hermes-arena-kappa.vercel.app/arena/docs.
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
#   - FLAT closes any open position for that symbol
#   - positionSizePercent: server caps 20%/trade, 60% total exposure
#   - Symbols you don't include keep their existing position
#   - Stop-loss / take-profit are server-side regardless

def decide(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Replace the body of this function with your strategy.

    The example below holds everything FLAT — useful as a no-op baseline so
    you can confirm the loop is wired correctly before plugging in real logic.
    """
    decisions: list[dict[str, Any]] = []
    for symbol in snap.get("coins", {}):
        decisions.append({
            "symbol": symbol,
            "action": "FLAT",
            "reason": "starter template — replace decide() with your logic",
            "positionSizePercent": 0,
        })
    return decisions


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
